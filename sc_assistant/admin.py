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
from aws.s3 import upload_file_to_s3, delete_file_from_s3, get_s3_presigned_url
from rag.chain import embeddings
from store_index import append_file_to_index
from .utils import is_admin, get_cognito_username

bp = Blueprint('admin', __name__)

def format_time_ago(dt_str):
    if not dt_str:
        return "just now"
    try:
        dt = datetime.datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        diff = now - dt
        seconds = diff.total_seconds()
        if seconds < 60: return "just now"
        elif seconds < 3600: return f"{int(seconds // 60)} minute{'s' if int(seconds // 60) > 1 else ''} ago"
        elif seconds < 86400: return f"{int(seconds // 3600)} hour{'s' if int(seconds // 3600) > 1 else ''} ago"
        else: return f"{int(seconds // 86400)} day{'s' if int(seconds // 86400) > 1 else ''} ago"
    except Exception as e:
        print(f"Error formatting time: {e}")
        return "a while ago"


@bp.route("/dashboard")
def dashboard():
    if not session.get("user") or not is_admin():
        return redirect(url_for("chat.chat_page"))
    user_obj = {
        "email": session.get("user"),
        "username": session.get("username", session.get("user", "").split("@")[0])
    }
    return render_template("dashboard.html", user=user_obj)


@bp.route("/upload", methods=["POST"])
def upload_file():
    if not session.get("user") or not is_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403
    if "files[]" not in request.files:
        return jsonify({"success": False, "message": "No file part"}), 400

    files = request.files.getlist("files[]")
    processed_files = []
    for file in files:
        if not file.filename: continue
        with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file.filename)[1]) as tmp:
            file.save(tmp.name)
            temp_path = tmp.name
        try:
            append_file_to_index(temp_path, index_name=INDEX_NAME, real_name=file.filename, embeddings=embeddings)
            save_file_metadata(file.filename, session.get("uid"))
            if not upload_file_to_s3(temp_path, file.filename):
                raise Exception("Failed to upload file to S3.")
            processed_files.append(file.filename)
        except Exception as e:
            print(f"Error processing {file.filename}: {e}")
            os.remove(temp_path)
            return jsonify({"success": False, "message": f"Error processing {file.filename}: {str(e)}"}), 500
        finally:
            if os.path.exists(temp_path): os.remove(temp_path)

    if processed_files:
        return jsonify({"success": True, "files": processed_files, "message": "Files uploaded & indexed successfully"})
    return jsonify({"success": False, "message": "No files uploaded"}), 400


@bp.route("/files", methods=["GET"])
def list_files():
    if not session.get("user") or not is_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403
    try:
        items = files_table.scan().get("Items", [])
        formatted_files = [{**item, 'name': item.pop('filename'), 'size': item.get('size', 0)} for item in items if 'filename' in item]
        return jsonify({"success": True, "files": formatted_files})
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            return jsonify({"success": True, "files": []})
        return jsonify({"success": False, "message": str(e)}), 500


@bp.route("/delete/<filename>", methods=["DELETE"])
def delete_file(filename):
    if not session.get("user") or not is_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403
    try:
        pc = Pinecone(api_key=PINECONE_API_KEY)
        index = pc.Index(INDEX_NAME)
        index.delete(filter={"source": {"$eq": filename}})
        delete_file_from_db(filename)
        delete_file_from_s3(filename)
        return jsonify({"success": True, "message": f"Deleted {filename} and its vectors"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@bp.route("/api/files/view-url/<filename>")
def get_view_url(filename):
    if not session.get("user"): return jsonify({"success": False, "message": "Unauthorized"}), 403
    try:
        url = get_s3_presigned_url(filename, for_download=False)
        if url: return jsonify({"success": True, "url": url})
        else: return jsonify({"success": False, "message": "Could not generate view URL."}), 500
    except Exception as e:
        return jsonify({"success": False, "message": f"An error occurred: {str(e)}"}), 500


@bp.route("/api/files/download-url/<filename>")
def get_download_url(filename):
    if not session.get("user"): return jsonify({"success": False, "message": "Unauthorized"}), 403
    try:
        url = get_s3_presigned_url(filename, for_download=True)
        if url: return jsonify({"success": True, "url": url})
        else: return jsonify({"success": False, "message": "Could not generate download URL."}), 500
    except Exception as e:
        return jsonify({"success": False, "message": f"An error occurred: {str(e)}"}), 500


@bp.route("/")
def root():
    if not session.get("user"):
        return redirect(url_for("auth.auth_page"))
    return redirect(url_for("admin.dashboard") if is_admin() else url_for("chat.chat_page"))


@bp.route("/api/dashboard/stats")
def get_dashboard_stats():
    if not session.get("user") or not is_admin(): return jsonify({"success": False, "message": "Unauthorized"}), 403
    try:
        user_pool_info = cognito_client.describe_user_pool(UserPoolId=COGNITO_USER_POOL_ID)
        users_count = user_pool_info['UserPool'].get('EstimatedNumberOfUsers', 0)
        
        docs_count = 0
        try:
            docs_count = files_table.scan(Select='COUNT')['Count']
        except ClientError as e:
            if e.response['Error']['Code'] != 'ResourceNotFoundException': raise
            
        convos_count = 0
        try:
            convos_count = conversations_table.scan(Select='COUNT')['Count']
        except ClientError as e:
            if e.response['Error']['Code'] != 'ResourceNotFoundException': raise
            
        stats = {"users": users_count, "conversations": convos_count, "documents": docs_count}
        return jsonify({"success": True, "data": stats})
    except ClientError as e:
        return jsonify({"success": False, "message": f"AWS Error: {e.response['Error']['Message']}"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": f"An error occurred: {str(e)}"}), 500


@bp.route("/api/dashboard/activities")
def get_recent_activities():
    if not session.get("user") or not is_admin(): return jsonify({"success": False, "message": "Unauthorized"}), 403
    activities = []
    try:
        try:
            response_files = files_table.scan(Limit=5)
            for f in response_files.get('Items', []):
                activities.append({'timestamp': f.get('uploaded_at'), 'description': f'New document uploaded: <strong>{f.get("filename", "N/A")}</strong>', 'icon': 'fa-file-upload', 'time': format_time_ago(f.get('uploaded_at'))})
        except ClientError as e:
            if e.response['Error']['Code'] != 'ResourceNotFoundException': raise
            
        try:
            response_convos = conversations_table.scan(Limit=5)
            for c in response_convos.get('Items', []):
                activities.append({'timestamp': c.get('created_at'), 'description': f'New conversation started: <strong>{c.get("title", "Untitled")}</strong>', 'icon': 'fa-comment', 'time': format_time_ago(c.get('created_at'))})
        except ClientError as e:
            if e.response['Error']['Code'] != 'ResourceNotFoundException': raise
            
        response_users = cognito_client.list_users(UserPoolId=COGNITO_USER_POOL_ID, Limit=5)
        for u in response_users.get('Users', []):
            username = next((attr['Value'] for attr in u.get('Attributes', []) if attr['Name'] == 'name'), 'unknown')
            create_date_str = u.get('UserCreateDate').isoformat()
            activities.append({'timestamp': create_date_str, 'description': f'New user registered: <strong>{username}</strong>', 'icon': 'fa-user-plus', 'time': format_time_ago(create_date_str)})

        activities.sort(key=lambda x: x['timestamp'] or '', reverse=True)
        return jsonify({"success": True, "activities": activities[:5]})
    except ClientError as e:
        return jsonify({"success": False, "message": f"AWS Error: {e.response['Error']['Message']}"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": f"An error occurred: {str(e)}"}), 500


@bp.route("/api/dashboard/users")
def get_dashboard_users():
    if not session.get("user") or not is_admin(): return jsonify({"success": False, "message": "Unauthorized"}), 403
    try:
        paginator = cognito_client.get_paginator('list_users')
        user_list = []
        for page in paginator.paginate(UserPoolId=COGNITO_USER_POOL_ID):
            for u in page.get('Users', []):
                user_data = {"id": u.get('Username'), "status": u.get('UserStatus'), "joined": u.get('UserCreateDate').isoformat()}
                username = ""
                email = ""
                role = "user"
                for attr in u.get('Attributes', []):
                    if attr['Name'] == 'name': username = attr['Value']
                    elif attr['Name'] == 'email': email = attr['Value']
                    elif attr['Name'] == 'custom:role': role = attr['Value']
                user_data["username"] = username or email.split('@')[0]
                user_data["email"] = email
                user_data["role"] = role
                user_list.append(user_data)
        user_list.sort(key=lambda x: x['joined'], reverse=True)
        return jsonify({"success": True, "users": user_list})
    except ClientError as e:
        return jsonify({"success": False, "message": f"AWS Error: {e.response['Error']['Message']}"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": f"An error occurred: {str(e)}"}), 500


@bp.route("/api/dashboard/users/update", methods=["POST"])
def update_user():
    if not session.get("user") or not is_admin(): return jsonify({"success": False, "message": "Unauthorized"}), 403
    try:
        cognito_username = request.form.get('cognito_username')
        new_username = request.form.get('username')
        new_role = request.form.get('role')
        if not all([cognito_username, new_username, new_role]): return jsonify({"success": False, "message": "Missing data"}), 400

        attributes_to_update = [{'Name': 'name', 'Value': new_username}, {'Name': 'custom:role', 'Value': new_role}]
        cognito_client.admin_update_user_attributes(UserPoolId=COGNITO_USER_POOL_ID, Username=cognito_username, UserAttributes=attributes_to_update)
        
        is_self_update = False
        current_admin_cognito_username = get_cognito_username()
        if cognito_username == current_admin_cognito_username:
            session['username'] = new_username
            session['role'] = new_role
            is_self_update = True

        return jsonify({"success": True, "message": "User updated successfully", "is_self_update": is_self_update, "new_username": new_username})
    except ClientError as e:
        return jsonify({"success": False, "message": f"AWS Error: {e.response['Error']['Message']}"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": f"An error occurred: {str(e)}"}), 500


@bp.route("/api/dashboard/users/delete", methods=["POST"])
def delete_user():
    if not session.get("user") or not is_admin(): return jsonify({"success": False, "message": "Unauthorized"}), 403
    try:
        cognito_username = request.json.get('cognito_username')
        if not cognito_username: return jsonify({"success": False, "message": "User ID is required"}), 400
        current_admin_username = get_cognito_username()
        if current_admin_username == cognito_username: return jsonify({"success": False, "message": "Cannot delete your own account from the admin dashboard."}), 400

        user_conversations = list_conversations(cognito_username)
        for conv in user_conversations: delete_conversation_from_db(cognito_username, conv['conv_id'])
        
        cognito_client.admin_delete_user(UserPoolId=COGNITO_USER_POOL_ID, Username=cognito_username)
        return jsonify({"success": True, "message": "User and all associated data deleted successfully."})
    except ClientError as e:
        return jsonify({"success": False, "message": f"AWS Error: {e.response['Error']['Message']}"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": f"An error occurred: {str(e)}"}), 500