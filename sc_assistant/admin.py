from flask import (
    Blueprint, render_template, jsonify, request, session, redirect, url_for
)
import os
import tempfile
import datetime
from botocore.exceptions import ClientError
from pinecone import Pinecone

from config import INDEX_NAME, PINECONE_API_KEY, COGNITO_USER_POOL_ID
from aws.cognito import cognito_client
from aws.dynamodb import (
    save_file_metadata, delete_file_from_db, files_table, conversations_table,
    list_conversations, delete_conversation_from_db
)
# --- NEW IMPORTS ---
from aws.s3 import upload_file_to_s3, delete_file_from_s3, get_s3_presigned_url
# --- END NEW IMPORTS ---
from rag.chain import embeddings
from store_index import append_file_to_index
from .utils import is_admin, get_cognito_username

bp = Blueprint('admin', __name__)

# --- Helper Function for Timestamps ---
# (format_time_ago function remains unchanged)
def format_time_ago(dt_str):
    """Converts an ISO 8601 string to a 'time ago' format."""
    if not dt_str:
        return "just now"
    try:
        # Parse the datetime string. Assumes UTC.
        dt = datetime.datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
            
        now = datetime.datetime.now(datetime.timezone.utc)
        diff = now - dt
        
        seconds = diff.total_seconds()
        
        if seconds < 60:
            return "just now"
        elif seconds < 3600:
            minutes = int(seconds // 60)
            return f"{minutes} minute{'s' if minutes > 1 else ''} ago"
        elif seconds < 86400:
            hours = int(seconds // 3600)
            return f"{hours} hour{'s' if hours > 1 else ''} ago"
        else:
            days = int(seconds // 86400)
            return f"{days} day{'s' if days > 1 else ''} ago"
            
    except Exception as e:
        print(f"Error formatting time: {e}")
        return "a while ago"

# --- Main Dashboard Route ---
# (dashboard route remains unchanged)
@bp.route("/dashboard")
def dashboard():
    if not session.get("user") or not is_admin():
        return redirect(url_for("chat.chat_page"))
    
    user_obj = {
        "email": session.get("user"),
        "username": session.get("username", session.get("user", "").split("@")[0])
    }
    
    return render_template("dashboard.html", user=user_obj)

# --- File Management Routes ---

@bp.route("/upload", methods=["POST"])
def upload_file():
    if not session.get("user") or not is_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403
    if "files[]" not in request.files:
        return jsonify({"success": False, "message": "No file part"}), 400

    files = request.files.getlist("files[]")
    processed_files = []
    for file in files:
        if not file.filename:
            continue
        # Use tempfile for secure file handling
        with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file.filename)[1]) as tmp:
            file.save(tmp.name)
            temp_path = tmp.name
        try:
            # 1. Process for RAG
            append_file_to_index(temp_path, index_name=INDEX_NAME, real_name=file.filename, embeddings=embeddings)
            
            # 2. Save metadata to DynamoDB
            save_file_metadata(file.filename, session.get("uid"))
            
            # 3. --- NEW: Upload original file to S3 ---
            if not upload_file_to_s3(temp_path, file.filename):
                raise Exception("Failed to upload file to S3.")
            
            processed_files.append(file.filename)
        except Exception as e:
            print(f"Error processing {file.filename}: {e}")
            # Clean up temp file on error
            os.remove(temp_path)
            return jsonify({"success": False, "message": f"Error processing {file.filename}: {str(e)}"}), 500
        finally:
            # Ensure temp file is always removed
            if os.path.exists(temp_path):
                os.remove(temp_path)

    if processed_files:
        return jsonify({"success": True, "files": processed_files, "message": "Files uploaded & indexed successfully"})
    return jsonify({"success": False, "message": "No files uploaded"}), 400


@bp.route("/files", methods=["GET"])
def list_files():
    # (list_files route remains unchanged)
    if not session.get("user") or not is_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    try:
        items = files_table.scan().get("Items", [])
        formatted_files = [
            {
                **item, 
                'name': item.pop('filename'), 
                'size': item.get('size', 0)
            } 
            for item in items if 'filename' in item
        ]
        return jsonify({"success": True, "files": formatted_files})
    except ClientError as e:
        return jsonify({"success": False, "message": str(e)}), 500

@bp.route("/delete/<filename>", methods=["DELETE"])
def delete_file(filename):
    if not session.get("user") or not is_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403
    try:
        pc = Pinecone(api_key=PINECONE_API_KEY)
        index = pc.Index(INDEX_NAME)
        
        # 1. Delete from Pinecone
        index.delete(filter={"source": {"$eq": filename}})
        
        # 2. Delete from DynamoDB
        delete_file_from_db(filename)
        
        # 3. --- NEW: Delete from S3 ---
        delete_file_from_s3(filename)
        
        return jsonify({"success": True, "message": f"Deleted {filename} and its vectors"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

# --- NEW: Routes for View/Download ---
@bp.route("/api/files/view-url/<filename>")
def get_view_url(filename):
    if not session.get("user") or not is_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403
    try:
        url = get_s3_presigned_url(filename, for_download=False)
        if url:
            return jsonify({"success": True, "url": url})
        else:
            return jsonify({"success": False, "message": "Could not generate view URL."}), 500
    except Exception as e:
        return jsonify({"success": False, "message": f"An error occurred: {str(e)}"}), 500

@bp.route("/api/files/download-url/<filename>")
def get_download_url(filename):
    if not session.get("user") or not is_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403
    try:
        url = get_s3_presigned_url(filename, for_download=True)
        if url:
            return jsonify({"success": True, "url": url})
        else:
            return jsonify({"success": False, "message": "Could not generate download URL."}), 500
    except Exception as e:
        return jsonify({"success": False, "message": f"An error occurred: {str(e)}"}), 500
# --- END NEW ---

# --- Root Redirect ---
# (root route remains unchanged)
@bp.route("/")
def root():
    if not session.get("user"):
        return redirect(url_for("auth.auth_page"))
    return redirect(url_for("admin.dashboard") if is_admin() else url_for("chat.chat_page"))

# --- Dashboard API Endpoints ---
# (All other API endpoints remain unchanged)
@bp.route("/api/dashboard/stats")
def get_dashboard_stats():
    if not session.get("user") or not is_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    try:
        # 1. Get User Count from Cognito
        user_pool_info = cognito_client.describe_user_pool(UserPoolId=COGNITO_USER_POOL_ID)
        users_count = user_pool_info['UserPool'].get('EstimatedNumberOfUsers', 0)
        
        # 2. Get Document Count from DynamoDB
        docs_count = files_table.scan(Select='COUNT')['Count']
        
        # 3. Get Conversation Count from DynamoDB
        convos_count = conversations_table.scan(Select='COUNT')['Count']
        
        stats = {
            "users": users_count,
            "conversations": convos_count,
            "documents": docs_count,
        }
        
        return jsonify({"success": True, "data": stats})
        
    except ClientError as e:
        return jsonify({"success": False, "message": f"AWS Error: {e.response['Error']['Message']}"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": f"An error occurred: {str(e)}"}), 500


@bp.route("/api/dashboard/activities")
def get_recent_activities():
    if not session.get("user") or not is_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403
    
    activities = []
    
    try:
        # 1. Get recent file uploads
        response_files = files_table.scan(Limit=5)
        for f in response_files.get('Items', []):
            activities.append({
                'timestamp': f.get('uploaded_at'),
                'description': f'New document uploaded: <strong>{f.get("filename", "N/A")}</strong>',
                'icon': 'fa-file-upload',
                'time': format_time_ago(f.get('uploaded_at'))
            })
            
        # 2. Get recent conversations
        response_convos = conversations_table.scan(Limit=5)
        for c in response_convos.get('Items', []):
            activities.append({
                'timestamp': c.get('created_at'),
                'description': f'New conversation started: <strong>{c.get("title", "Untitled")}</strong>',
                'icon': 'fa-comment',
                'time': format_time_ago(c.get('created_at'))
            })
            
        # 3. Get recent user signups
        response_users = cognito_client.list_users(UserPoolId=COGNITO_USER_POOL_ID, Limit=5)
        for u in response_users.get('Users', []):
            username = next((attr['Value'] for attr in u.get('Attributes', []) if attr['Name'] == 'name'), 'unknown')
            create_date_str = u.get('UserCreateDate').isoformat()
            activities.append({
                'timestamp': create_date_str,
                'description': f'New user registered: <strong>{username}</strong>',
                'icon': 'fa-user-plus',
                'time': format_time_ago(create_date_str)
            })

        # Sort all combined activities by timestamp (newest first)
        activities.sort(key=lambda x: x['timestamp'] or '', reverse=True)
        
        # Return the top 5 most recent activities
        return jsonify({"success": True, "activities": activities[:5]})

    except ClientError as e:
        return jsonify({"success": False, "message": f"AWS Error: {e.response['Error']['Message']}"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": f"An error occurred: {str(e)}"}), 500

@bp.route("/api/dashboard/users")
def get_dashboard_users():
    if not session.get("user") or not is_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    try:
        paginator = cognito_client.get_paginator('list_users')
        user_list = []
        
        for page in paginator.paginate(UserPoolId=COGNITO_USER_POOL_ID):
            for u in page.get('Users', []):
                user_data = {
                    "id": u.get('Username'), # This is the 'sub'
                    "status": u.get('UserStatus'),
                    "joined": u.get('UserCreateDate').isoformat()
                }
                
                # Extract attributes
                username = ""
                email = ""
                role = "user" # Default role
                for attr in u.get('Attributes', []):
                    if attr['Name'] == 'name':
                        username = attr['Value']
                    elif attr['Name'] == 'email':
                        email = attr['Value']
                    elif attr['Name'] == 'custom:role': # Get custom role
                        role = attr['Value']
                
                user_data["username"] = username or email.split('@')[0]
                user_data["email"] = email
                user_data["role"] = role # Add role
                
                user_list.append(user_data)
        
        # Sort by join date, newest first
        user_list.sort(key=lambda x: x['joined'], reverse=True)
        
        return jsonify({"success": True, "users": user_list})

    except ClientError as e:
        return jsonify({"success": False, "message": f"AWS Error: {e.response['Error']['Message']}"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": f"An error occurred: {str(e)}"}), 500

@bp.route("/api/dashboard/users/update", methods=["POST"])
def update_user():
    if not session.get("user") or not is_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    try:
        cognito_username = request.form.get('cognito_username') # This is the user's 'sub'
        new_username = request.form.get('username')
        new_role = request.form.get('role')

        if not all([cognito_username, new_username, new_role]):
            return jsonify({"success": False, "message": "Missing data"}), 400

        # 1. Update User Attributes
        attributes_to_update = [
            {'Name': 'name', 'Value': new_username},
            {'Name': 'custom:role', 'Value': new_role}
        ]
        cognito_client.admin_update_user_attributes(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=cognito_username,
            UserAttributes=attributes_to_update
        )
        
        # --- Check if updating self and update session ---
        is_self_update = False
        current_admin_cognito_username = get_cognito_username()
        if cognito_username == current_admin_cognito_username:
            session['username'] = new_username
            session['role'] = new_role
            is_self_update = True
        # --- END NEW ---

        return jsonify({
            "success": True, 
            "message": "User updated successfully",
            "is_self_update": is_self_update,
            "new_username": new_username
        })

    except ClientError as e:
        return jsonify({"success": False, "message": f"AWS Error: {e.response['Error']['Message']}"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": f"An error occurred: {str(e)}"}), 500

@bp.route("/api/dashboard/users/delete", methods=["POST"])
def delete_user():
    if not session.get("user") or not is_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    try:
        cognito_username = request.json.get('cognito_username') # This is the 'sub'
        if not cognito_username:
            return jsonify({"success": False, "message": "User ID is required"}), 400
        
        # Safety check: prevent admin from deleting themselves
        current_admin_username = get_cognito_username()
        if current_admin_username == cognito_username:
            return jsonify({"success": False, "message": "Cannot delete your own account from the admin dashboard."}), 400

        # 1. Delete all conversations from DynamoDB
        # Note: The 'uid' used in DynamoDB is the user's 'sub' (cognito_username)
        user_conversations = list_conversations(cognito_username)
        for conv in user_conversations:
            delete_conversation_from_db(cognito_username, conv['conv_id'])
        
        # 2. Delete user from Cognito
        cognito_client.admin_delete_user(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=cognito_username
        )
        
        return jsonify({"success": True, "message": "User and all associated data deleted successfully."})

    except ClientError as e:
        return jsonify({"success": False, "message": f"AWS Error: {e.response['Error']['Message']}"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": f"An error occurred: {str(e)}"}), 500