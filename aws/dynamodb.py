import datetime
import hashlib
import uuid

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from aws.dynamodb_access import ThreadLocalTable

files_table = ThreadLocalTable("Files")
conversations_table = ThreadLocalTable("Conversations")
reports_table = ThreadLocalTable("SCAssistantReports")


# ── Files ────────────────────────────────────────────────────

def save_file_metadata(filename, uid):
    """Saves file metadata to DynamoDB."""
    files_table.put_item(Item={
        "filename":    filename,
        "uploaded_by": uid,
        "uploaded_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
    })


def get_file_upload_dates():
    """Read document chronology only, without uploader identities or file content."""
    uploads = {}
    arguments = {"ProjectionExpression": "filename, uploaded_at", "ConsistentRead": True}
    while True:
        result = files_table.scan(**arguments)
        for item in result.get("Items", []):
            if isinstance(item.get("filename"), str) and isinstance(item.get("uploaded_at"), str):
                uploads[item["filename"]] = item["uploaded_at"]
        last_key = result.get("LastEvaluatedKey")
        if not last_key:
            return uploads
        arguments["ExclusiveStartKey"] = last_key


# ── Conversations ────────────────────────────────────────────

def upsert_conversation(uid, conv_id, history, created_at):
    """Creates or updates a conversation in DynamoDB."""
    if not history:
        return
    title = next((m["content"][:40] for m in history if m["role"] == "user"), "Untitled Chat")
    item = {
        "conv_id":    conv_id,
        "uid":        uid,
        "messages":   history,
        "title":      title,
        "created_at": created_at,
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }
    conversations_table.put_item(Item=item)


def list_conversations(uid):
    """Lists all conversations for a given user."""
    resp = conversations_table.query(
        IndexName="uid-index",
        KeyConditionExpression=Key("uid").eq(uid),
        ScanIndexForward=False
    )
    return resp.get("Items", [])


def record_guest_conversation(session_id):
    """Create one opaque count marker per guest chat, without saving its messages."""
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("A guest conversation identifier is required.")
    marker = hashlib.sha256(("guest-conversation:" + session_id).encode("utf-8")).hexdigest()
    try:
        conversations_table.put_item(
            Item={
                "uid": "guest",
                "conv_id": "guest-" + marker,
                "conversation_type": "guest",
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            },
            ConditionExpression="attribute_not_exists(conv_id) AND attribute_not_exists(uid)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def count_conversations():
    """Count account chats and guest markers across all pages, without returning messages."""
    total = 0
    scan_args = {"Select": "COUNT", "ConsistentRead": True}
    while True:
        page = conversations_table.scan(**scan_args)
        total += page["Count"]
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            return total
        scan_args["ExclusiveStartKey"] = last_key


def get_conversation(uid, conv_id):
    """Retrieves a specific conversation from DynamoDB."""
    resp = conversations_table.get_item(Key={"conv_id": conv_id, "uid": uid}, ConsistentRead=True)
    return resp.get("Item")


def delete_conversation_from_db(uid, conv_id):
    """Deletes a conversation from DynamoDB."""
    conversations_table.delete_item(Key={"conv_id": conv_id, "uid": uid})


def delete_file_from_db(filename):
    """Deletes a file's metadata from DynamoDB."""
    files_table.delete_item(Key={"filename": filename})


# ── Reports ──────────────────────────────────────────────────

def save_report(data: dict) -> str:
    """
    Save a student report to DynamoDB. Returns the new report_id.

    Reports are filed when a student chooses a reason, independently of votes.
    A repeated submission updates the reason of an existing report for that
    answer when it is found, rather than intentionally creating another row.
    """
    msg_id = data.get("msg_id", "")
    existing = find_report_by_msg(msg_id) if msg_id else None
    if existing:
        update_report_reason(
            existing["report_id"],
            data.get("reason", ""),
            data.get("other_text", ""),
        )
        return existing["report_id"]

    report_id = str(uuid.uuid4())
    reports_table.put_item(Item={
        "report_id":      report_id,
        "reporter_email": data.get("reporter_email", ""),
        "reporter_uid":   data.get("reporter_uid", ""),
        "conv_id":        data.get("conv_id") or "",
        "msg_id":         msg_id,
        "reason":         data.get("reason", ""),
        "other_text":     data.get("other_text", ""),
        "msg_snippet":    data.get("msg_snippet", ""),
        "question":       data.get("question", ""),
        # The files and pages the answer was built from. A report that names the
        # document is a work order; one that does not is a complaint the admin
        # has to reproduce by hand.
        "sources":        data.get("sources") or [],
        "source":         data.get("source", "report"),
        "status":         "pending",   # pending | done | ignored
        "created_at":     datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "resolved_at":    "",
    })
    return report_id


def find_report_by_msg(msg_id: str):
    """The existing report for one answer, if the student already flagged it."""
    if not msg_id:
        return None
    for r in list_reports():
        if r.get("msg_id") == msg_id:
            return r
    return None


def update_report_reason(report_id: str, reason: str, other_text: str = ""):
    """
    Attach a reason to a report that was filed without one.

    Only ever fills in detail — an empty reason does not wipe what the student
    already said, because the sheet can be dismissed after being answered and a
    dismissal must not delete information.
    """
    if not reason and not other_text:
        return
    reports_table.update_item(
        Key={"report_id": report_id},
        UpdateExpression="SET #r = :r, other_text = :o",
        ExpressionAttributeNames={"#r": "reason"},
        ExpressionAttributeValues={":r": reason, ":o": other_text},
    )


def list_reports(status_filter: str = None) -> list:
    """List all reports, optionally filtered by status."""
    resp  = reports_table.scan()
    items = resp.get("Items", [])
    # Handle DynamoDB pagination
    while "LastEvaluatedKey" in resp:
        resp   = reports_table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items += resp.get("Items", [])
    if status_filter:
        items = [r for r in items if r.get("status") == status_filter]
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return items


def update_report_status(report_id: str, status: str):
    """Update report status to 'done' or 'ignored'."""
    reports_table.update_item(
        Key={"report_id": report_id},
        UpdateExpression="SET #s = :s, resolved_at = :r",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": status,
            ":r": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
    )


