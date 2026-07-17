import boto3
import mimetypes
from botocore.exceptions import ClientError
from botocore.client import Config
from config import S3_BUCKET_NAME, AWS_REGION

# Explicitly set the region and signature version so URLs work on EC2!
s3_client = boto3.client(
    's3',
    region_name=AWS_REGION,
    config=Config(signature_version='s3v4')
)

def upload_file_to_s3(file_path, object_name):
    """
    Upload a file to an S3 bucket.
    
    :param file_path: File to upload
    :param object_name: S3 object name (filename)
    :return: True if upload was successful, else False
    """
    try:
        # --- NEW: Guess MIME type ---
        mimetype, _ = mimetypes.guess_type(object_name)
        if mimetype is None:
            mimetype = 'application/octet-stream' # Fallback
            
        extra_args = {'ContentType': mimetype}
        # --- END NEW ---
        
        s3_client.upload_file(
            file_path, 
            S3_BUCKET_NAME, 
            object_name,
            ExtraArgs=extra_args  # --- NEW: Add ExtraArgs ---
        )
    except ClientError as e:
        print(f"Error uploading to S3: {e}")
        return False
    return True

def delete_file_from_s3(object_name):
    """
    Delete an object from an S3 bucket.
    
    :param object_name: S3 object name (filename)
    :return: True if delete was successful, else False
    """
    try:
        s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=object_name)
    except ClientError as e:
        print(f"Error deleting from S3: {e}")
        return False
    return True

def get_s3_presigned_url(object_name, for_download=False, expiration=3600):
    s3_client = boto3.client('s3') # Add your credentials/region as needed
    
    # 1. Guess the file type (e.g., 'application/pdf' for PDFs)
    content_type, _ = mimetypes.guess_type(object_name)
    if not content_type:
        content_type = 'application/octet-stream' # Fallback
        
    # 2. Set to 'inline' for viewing, 'attachment' for downloading
    disposition = 'attachment' if for_download else 'inline'
    
    # 3. Pass these headers into the Params dictionary
    params = {
        'Bucket': S3_BUCKET_NAME,
        'Key': object_name,
        'ResponseContentDisposition': f'{disposition}; filename="{object_name}"',
        'ResponseContentType': content_type
    }
    
    try:
        response = s3_client.generate_presigned_url(
            'get_object',
            Params=params,
            ExpiresIn=expiration
        )
        return response
    except Exception as e:
        print(f"Error generating presigned URL: {e}")
        return None