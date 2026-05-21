import boto3
import datetime
import uuid
from boto3.dynamodb.conditions import Key
from config import AWS_REGION

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
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


def get_conversation(uid, conv_id):
    """Retrieves a specific conversation from DynamoDB."""
    resp = conversations_table.get_item(Key={"conv_id": conv_id, "uid": uid})
    return resp.get("Item")


def delete_conversation_from_db(uid, conv_id):
    """Deletes a conversation from DynamoDB."""
    conversations_table.delete_item(Key={"conv_id": conv_id, "uid": uid})


def delete_file_from_db(filename):
    """Deletes a file's metadata from DynamoDB."""
    files_table.delete_item(Key={"filename": filename})


# ── Reports ──────────────────────────────────────────────────

def save_report(data: dict) -> str:
    """Save a student report to DynamoDB. Returns the new report_id."""
    report_id = str(uuid.uuid4())
    reports_table.put_item(Item={
        "report_id":      report_id,
        "reporter_email": data.get("reporter_email", ""),
        "reporter_uid":   data.get("reporter_uid", ""),
        "conv_id":        data.get("conv_id") or "",
        "msg_id":         data.get("msg_id", ""),
        "reason":         data.get("reason", ""),
        "other_text":     data.get("other_text", ""),
        "msg_snippet":    data.get("msg_snippet", ""),
        "status":         "pending",   # pending | done | ignored
        "created_at":     datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "resolved_at":    "",
    })
    return report_id


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