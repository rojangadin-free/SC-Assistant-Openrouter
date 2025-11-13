from flask import (
    Blueprint, render_template, jsonify, request, session, redirect, url_for
)
from botocore.exceptions import ClientError
from config import COGNITO_USER_POOL_ID
from aws.cognito import login_user, cognito_client
from aws.dynamodb import (
    list_conversations, delete_conversation_from_db
)
from .utils import get_cognito_username

bp = Blueprint('settings', __name__, url_prefix='/settings')

@bp.route("/")
def settings_page():
    if not session.get("user"):
        return redirect(url_for("auth.auth_page"))
    
    user_obj = {
        "email": session.get("user"),
        "username": session.get("username", session.get("user", "").split("@")[0])
    }
    
    return render_template("settings.html", user=user_obj)

@bp.route("/update-profile", methods=["POST"])
def update_profile():
    if not session.get("user"):
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    new_username = request.form.get("username", "").strip()
    current_username = session.get("username", "")
    
    if not new_username:
        return jsonify({"success": False, "message": "Username is required"})
    if len(new_username) < 3:
        return jsonify({"success": False, "message": "Username must be at least 3 characters long"})
    if "@" in new_username or ' ' in new_username:
        return jsonify({"success": False, "message": "Username cannot be an email address or contain spaces"})
    if new_username == current_username:
        return jsonify({"success": True, "message": "No changes made"})
    
    try:
        cognito_username = get_cognito_username()
        
        cognito_client.admin_update_user_attributes(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=cognito_username,
            UserAttributes=[
                {'Name': 'name', 'Value': new_username}
            ]
        )
        session["username"] = new_username
        
        return jsonify({
            "success": True, 
            "message": "Username updated successfully!"
        })
        
    except ClientError as e:
        error_code = e.response['Error']['Code']
        return jsonify({"success": False, "message": f"An AWS error occurred: {error_code}"})
    except Exception as e:
        return jsonify({"success": False, "message": f"An unexpected error occurred: {str(e)}"})

@bp.route("/change-password", methods=["POST"])
def change_password():
    if not session.get("user"):
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    current_password = request.form.get("current_password")
    new_password = request.form.get("new_password")

    if not all([current_password, new_password]):
        return jsonify({"success": False, "message": "Both current and new passwords are required"})
    if len(new_password) < 6:
        return jsonify({"success": False, "message": "New password must be at least 6 characters long"})

    login_result = login_user(session.get("user"), current_password)
    if not login_result["success"]:
        return jsonify({"success": False, "message": "Current password is incorrect"})

    try:
        cognito_username = get_cognito_username()
        cognito_client.admin_set_user_password(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=cognito_username,
            Password=new_password,
            Permanent=True
        )
        return jsonify({"success": True, "message": "Password changed successfully"})
        
    except ClientError as e:
        error_code = e.response['Error']['Code']
        return jsonify({"success": False, "message": f"An AWS error occurred: {error_code}"})
    except Exception as e:
        return jsonify({"success": False, "message": f"An unexpected error occurred: {str(e)}"})

@bp.route("/delete-account", methods=["DELETE"])
def delete_account():
    if not session.get("user"):
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    
    user_id = session.get("uid")
    cognito_username = get_cognito_username()

    try:
        conversations = list_conversations(user_id)
        for conv in conversations:
            delete_conversation_from_db(user_id, conv['conv_id'])

        cognito_client.admin_delete_user(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=cognito_username
        )
        session.clear()
        
        return jsonify({"success": True, "message": "Account deleted successfully"})

    except ClientError as e:
        error_code = e.response['Error']['Code']
        return jsonify({"success": False, "message": f"An AWS error occurred: {error_code}"})
    except Exception as e:
        return jsonify({"success": False, "message": f"An unexpected error occurred: {str(e)}"})