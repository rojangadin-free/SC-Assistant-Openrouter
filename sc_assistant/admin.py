from flask import (
    Blueprint, render_template, jsonify, request, session, redirect, url_for
)
import os
import tempfile
import threading
import datetime
from botocore.exceptions import ClientError
from pinecone import Pinecone


from config import INDEX_NAME, PINECONE_API_KEY, COGNITO_USER_POOL_ID
from aws.cognito import cognito_client
from aws.dynamodb import (
    save_file_metadata, delete_file_from_db, files_table,
    list_conversations, delete_conversation_from_db
)

from aws.s3 import upload_file_to_s3, delete_file_from_s3, get_s3_presigned_url
from rag.chain import embeddings
from rag import jobs
from rag import activity
from store_index import append_file_to_index
from .utils import is_admin, get_cognito_username


bp = Blueprint('admin', __name__)

# `format_time_ago` used to live here. It has moved to `rag.activity.humanize`,
# next to the code that reads the timestamps, and gained the two things this
# version lacked: a switch to an absolute date past a week ("34 days ago" is
# arithmetic the reader has to do), and Manila time rather than UTC — this one
# compared a Manila-stamped event against `utcnow()`, so anything logged in the
# last eight hours was described as being in the future.



@bp.route("/dashboard")
def dashboard():
    if not session.get("user") or not is_admin():
        return redirect(url_for("chat.chat_page"))
    user_obj = {
        "email": session.get("user"),
        "username": session.get("username", session.get("user", "").split("@")[0])
    }
    return render_template("dashboard.html", user=user_obj)


def _index_uploaded_files(job_id, staged, uid):
    """
    The indexing worker. Runs off the request thread.

    Each file is independent: one corrupt PDF must not abandon the files queued
    behind it, so failures are recorded per file and the loop continues. That is
    also why the job's final status is derived from its files rather than from
    the first exception.
    """
    for temp_path, filename in staged:
        try:
            def _progress(phase, done, total, _f=filename):
                jobs.set_phase(job_id, _f, phase, units_done=done, units_total=total)

            chunks = append_file_to_index(
                temp_path,
                index_name=INDEX_NAME,
                real_name=filename,
                embeddings=embeddings,
                progress=_progress,
            )

            # S3 and the metadata row come after a successful index. Ordered this
            # way so a file that cannot be indexed never appears in the file list
            # as though it were searchable — the list is the admin's evidence that
            # the assistant can actually use a document.
            jobs.set_phase(job_id, filename, "storing")
            save_file_metadata(filename, uid)
            if not upload_file_to_s3(temp_path, filename):
                raise Exception("Failed to upload file to S3.")

            jobs.finish_file(job_id, filename, chunks)
        except Exception as e:
            print(f"Error processing {filename}: {e}")
            jobs.fail_file(job_id, filename, str(e))
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

    jobs.finish_job(job_id)


@bp.route("/upload", methods=["POST"])
def upload_file():
    """
    Accept the upload, then index it in the background.

    The old version did the whole pipeline inline and returned only when the last
    chunk was upserted. Two things were wrong with that. The browser's bar tracked
    bytes sent, so it read 100% while OCR had not started — it was measuring the
    wrong quantity. And gunicorn's `--timeout 120` would kill the worker partway
    through a large scanned PDF, reporting failure for a file whose vectors were
    already partly in Pinecone.

    Now the request only stages the files to disk (fast, bounded) and hands back a
    `job_id`. Progress is polled from `/upload/status/<job_id>`.
    """
    if not session.get("user") or not is_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403
    if "files[]" not in request.files:
        return jsonify({"success": False, "message": "No file part"}), 400

    files = [f for f in request.files.getlist("files[]") if f.filename]
    if not files:
        return jsonify({"success": False, "message": "No files uploaded"}), 400

    # Staged inside the request because `request.files` is only readable here —
    # the stream is tied to the request and would be closed by the time a worker
    # thread got to it.
    staged = []
    for file in files:
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=os.path.splitext(file.filename)[1]
        ) as tmp:
            file.save(tmp.name)
            staged.append((tmp.name, file.filename))

    job_id = jobs.create_job([name for _, name in staged])

    # `session` is not available inside the thread, so the uid is read now and
    # passed in. Reading it in the worker would raise "working outside of request
    # context" — and only for uploads slow enough to matter, which is the worst
    # kind of bug to ship.
    uid = session.get("uid")

    # daemon=True so a shutdown is not blocked by an in-flight index. A killed
    # index is recoverable (the job goes stale and says so); a container that
    # refuses to stop is not.
    threading.Thread(
        target=_index_uploaded_files,
        args=(job_id, staged, uid),
        daemon=True,
    ).start()

    # 202: accepted, not complete. The client must poll.
    return jsonify({
        "success": True,
        "job_id": job_id,
        "files": [name for _, name in staged],
        "message": "Upload received — indexing started.",
    }), 202


@bp.route("/upload/status/<job_id>")
def upload_status(job_id):
    """
    Progress for one indexing job.

    404 when unknown, which for the client means "stop polling". Jobs are pruned
    after `MAX_JOBS`, so a very old job id legitimately disappears; the client
    treats that as terminal rather than retrying forever.
    """
    if not session.get("user") or not is_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    job = jobs.get_job(job_id)
    if not job:
        return jsonify({"success": False, "message": "Unknown job."}), 404
    return jsonify({"success": True, "job": job})



@bp.route("/files", methods=["GET"])
def list_files():
    if not session.get("user") or not is_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403
    try:
        # ConsistentRead because this endpoint is called the instant an upload
        # finishes. A default DynamoDB scan is eventually consistent, so the row
        # save_file_metadata() just wrote is often missing from the response —
        # which looked exactly like "the upload worked but the list is empty
        # until you reload the page". A strongly consistent read costs twice the
        # capacity, which is irrelevant for a table holding one row per document.
        items = files_table.scan(ConsistentRead=True).get("Items", [])
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


# `/api/dashboard/stats` used to live here: total users, conversations and
# documents, which the Overview printed in three big tiles next to a hardcoded
# "+0% from last month".
#
# It is deleted rather than left unused because nothing on the rebuilt Overview
# asks a size question. Those three numbers cannot be acted on — knowing there
# are 412 conversations tells an admin nothing to do — and buying them cost a
# describe_user_pool plus two full-table COUNT scans on every dashboard load. The
# triage queue that replaced them answers "what is waiting for me", which is the
# only question a landing screen is worth loading for.
#
# Corpus-size figures still exist for the one place they are genuinely useful:
# /admin/analytics/api/documents, where they sit beside the per-document
# refusal rates that give them meaning.


def _dynamo_activity_rows():

    """
    Uploads and new accounts, shaped for `rag.activity.feed(extra=...)`.

    These two live in DynamoDB and Cognito rather than in a JSON store, so they
    cannot be read by `rag/activity.py` — it is deliberately import-safe without
    AWS credentials. They are handed over as pre-shaped rows and merged into the
    same time sort as everything else.

    NOTE ON `scan()`: this walks the whole Files table instead of asking for the
    "newest 5". That is not laziness, it is the fix. The previous version called
    `files_table.scan(Limit=5)` believing it returned the five most recent
    uploads. It does not — a scan has no ordering, and `Limit` only stops it
    early, so it returned five *arbitrary* rows which were then sorted among
    themselves. A document uploaded a minute ago showed up only if it happened to
    be one of the first five rows the scan walked past.

    Ordering by time in DynamoDB would need a sort key on the timestamp (or a GSI)
    and this table is keyed by filename, so the honest options are "read it all
    and sort" or "change the schema". At this scale — one row per uploaded
    document — reading it all is cheap and always correct. If the table ever grows
    past a few thousand rows, add a GSI on `uploaded_at` and query it backwards;
    the shape returned here would not change.

    Never raises: a missing table or an AWS permissions problem must cost these
    rows only, not the whole feed.
    """
    rows = []

    try:
        for f in files_table.scan().get("Items", []):
            rows.append({
                "at": f.get("uploaded_at") or "",
                "icon": "fa-file-arrow-up",
                "tone": "info",
                "text": "New document uploaded: "
                        f"<strong>{f.get('filename', 'unknown')}</strong>",
                "section": "uploads",
            })
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            print(f"  [dashboard] file rows unavailable (non-fatal): {e}")
    except Exception as e:
        print(f"  [dashboard] file rows unavailable (non-fatal): {e}")

    # New accounts. Cognito's list_users has the same problem as the scan — no
    # ordering — but it does support a page size, and 60 accounts is plenty to
    # find anything inside the feed's window without walking a large pool.
    try:
        listed = cognito_client.list_users(UserPoolId=COGNITO_USER_POOL_ID, Limit=60)
        for u in listed.get("Users", []):
            created = u.get("UserCreateDate")
            name = next((a["Value"] for a in u.get("Attributes", [])
                         if a["Name"] == "name"), "")
            email = next((a["Value"] for a in u.get("Attributes", [])
                          if a["Name"] == "email"), "")
            rows.append({
                "at": created.isoformat() if created else "",
                "icon": "fa-user-plus",
                "tone": "info",
                "text": "New account: "
                        f"<strong>{name or email or u.get('Username', 'unknown')}</strong>",
                "section": "users",
            })
    except Exception as e:
        print(f"  [dashboard] account rows unavailable (non-fatal): {e}")

    # Conversations are deliberately absent. "New conversation started: Untitled"
    # was the bulk of the old feed and told an admin nothing they could act on —
    # and the interesting half of a conversation (a question that went unanswered,
    # an answer nobody found helpful) already arrives through the gap and feedback
    # stores with the actual question attached.
    return rows


@bp.route("/api/dashboard/activities")
def get_recent_activities():
    """
    The real timeline, newest first. See `rag/activity.py` for why this no longer
    reads five arbitrary DynamoDB rows and calls them recent.
    """
    if not session.get("user") or not is_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    try:
        limit = max(1, min(int(request.args.get("limit", 10)), 50))
    except (TypeError, ValueError):
        limit = 10

    try:
        rows = activity.feed(limit, extra=_dynamo_activity_rows())
        return jsonify({
            "success": True,
            "activities": rows,
            # So the empty state can say "nothing in the last 30 days" rather
            # than the ambiguous "no activity".
            "window_days": activity.DEFAULT_WINDOW_DAYS,
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"An error occurred: {e}"}), 500


@bp.route("/api/dashboard/attention")
def get_dashboard_attention():
    """
    What is waiting for a human, and two honest health numbers.

    This is what the Overview screen leads with now. The old three cards counted
    users, conversations and documents — all real numbers, none of them
    answering "what should I do today".
    """
    if not session.get("user") or not is_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403
    try:
        return jsonify({
            "success": True,
            "attention": activity.attention(),
            "health": activity.health(),
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"An error occurred: {e}"}), 500



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