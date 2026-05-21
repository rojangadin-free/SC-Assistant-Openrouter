import boto3
import os
from botocore.exceptions import ClientError
from config import AWS_REGION # This triggers the dotenv load from your config

def create_reports_table():
    """
    Creates the SCAssistantReports DynamoDB table using credentials 
    loaded from environment variables or .env.
    """
    # Explicitly pull keys if they aren't in the default AWS search path
    session = boto3.Session(
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name=AWS_REGION
    )
    dynamodb = session.client("dynamodb")

    try:
        print(f"Creating SCAssistantReports table in {AWS_REGION}...")
        
        dynamodb.create_table(
            TableName="SCAssistantReports",
            AttributeDefinitions=[
                {"AttributeName": "report_id", "AttributeType": "S"},
                {"AttributeName": "status",    "AttributeType": "S"},
                {"AttributeName": "created_at","AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "report_id", "KeyType": "HASH"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "status-created-index",
                    "KeySchema": [
                        {"AttributeName": "status",     "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        
        waiter = dynamodb.get_waiter('table_exists')
        waiter.wait(TableName='SCAssistantReports')
        print("✅ SCAssistantReports table created successfully.")

    except dynamodb.exceptions.ResourceInUseException:
        print("ℹ️  Table 'SCAssistantReports' already exists — skipping.")
    except ClientError as e:
        print(f"❌ AWS Client Error: {e.response['Error']['Message']}")
    except Exception as e:
        print(f"❌ An unexpected error occurred: {e}")

if __name__ == "__main__":
    create_reports_table()