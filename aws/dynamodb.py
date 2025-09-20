import boto3
import datetime
from boto3.dynamodb.conditions import Key
from config import AWS_REGION

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
files_table = dynamodb.Table("Files")
conversations_table = dynamodb.Table("Conversations")

def save_file_metadata(filename, uid):
    """Saves file metadata to DynamoDB."""
    files_table.put_item(Item={
        "filename": filename,
        "uploaded_by": uid,
        "uploaded_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
    })

def upsert_conversation(uid, conv_id, history, created_at):
    """Creates or updates a conversation in DynamoDB."""
    if not history:
        return
    title = next((m["content"][:40] for m in history if m["role"] == "user"), "Untitled Chat")
    item = {
        "conv_id": conv_id,
        "uid": uid,
        "messages": history,
        "title": title,
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