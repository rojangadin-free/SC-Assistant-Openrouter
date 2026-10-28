import boto3
import datetime
import uuid
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError
from config import AWS_REGION

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION, config=Config(
    connect_timeout=3, read_timeout=10, retries={"max_attempts": 2}, max_pool_connections=32
))
files_table         = dynamodb.Table("Files")
conversations_table = dynamodb.Table("Conversations")
reports_table       = dynamodb.Table("SCAssistantReports")


# ── Files ────────────────────────────────────────────────────

def save_file_metadata(filename, uid):
    """Saves file metadata to DynamoDB."""
    files_table.put_item(Item={
        "filename":    filename,
        "uploaded_by": uid,
        "uploaded_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
    })


# ── Conversations ────────────────────────────────────────────

def upsert_conversation(uid, conv_id, history, created_at):
    """Atomically append a completed turn, without replacing another worker's turn."""
    if not history:
        return
    title = next((m["content"][:40] for m in history if m["role"] == "user"), "Untitled Chat")
    message_id = history[-1].get("msg_id")
    if not message_id:
        raise ValueError("A completed assistant turn requires a stable message ID")
    try:
        conversations_table.update_item(
            Key={"conv_id": conv_id, "uid": uid},
            UpdateExpression=(
                "SET #messages = list_append(if_not_exists(#messages, :empty), :turn), "
                "title = if_not_exists(title, :title), "
                "created_at = if_not_exists(created_at, :created), updated_at = :now "
                "ADD #turn_ids :turn_ids"
            ),
            ConditionExpression="attribute_not_exists(#turn_ids) OR NOT contains(#turn_ids, :message_id)",
            ExpressionAttributeNames={"#messages": "messages", "#turn_ids": "turn_ids"},
            ExpressionAttributeValues={
                ":empty": [], ":turn": history, ":title": title, ":created": created_at,
                ":now": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                ":message_id": message_id, ":turn_ids": {message_id},
            },
        )
    except ClientError as exc:
        # A timed-out successful UpdateItem may be retried by the SDK. Its
        # stable turn ID prevents a second append without losing other turns.
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise


def list_conversations(uid):
    """Lists all conversations for a given user."""
    resp = conversations_table.query(
        IndexName="uid-index",
        KeyConditionExpression=Key("uid").eq(uid),
        ScanIndexForward=False
    )
    items = resp.get("Items", [])
    while resp.get("LastEvaluatedKey"):
        resp = conversations_table.query(
            IndexName="uid-index", KeyConditionExpression=Key("uid").eq(uid),
            ScanIndexForward=False, ExclusiveStartKey=resp["LastEvaluatedKey"],
        )
        items.extend(resp.get("Items", []))
    for item in items:
        item.pop("turn_ids", None)
    return items


def get_conversation(uid, conv_id):
    """Retrieves a specific conversation from DynamoDB."""
    resp = conversations_table.get_item(Key={"conv_id": conv_id, "uid": uid}, ConsistentRead=True)
    item = resp.get("Item")
    if item:
        item.pop("turn_ids", None)
    return item


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

    `msg_id` doubles as an idempotency key. A downvote files the report
    automatically, and the student may then pick a reason from the sheet that
    follows — two requests about one answer. Scanning for an existing row keeps
    that as one report the admin has to read once, instead of a duplicate pair
    where the second copy holds the detail and the first is noise.
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
        "source":         data.get("source", "downvote"),  # downvote | manual
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


