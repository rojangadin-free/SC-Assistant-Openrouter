import boto3
import mimetypes # --- NEW IMPORT ---
from botocore.exceptions import ClientError
from config import S3_BUCKET_NAME

s3_client = boto3.client('s3')

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
    """
    Generate a presigned URL to securely access an S3 object.
    
    :param object_name: S3 object name (filename)
    :param for_download: If True, add headers to force download
    :param expiration: Time in seconds for the URL to be valid
    :return: Presigned URL as string, or None if error
    """
    try:
        params = {
            'Bucket': S3_BUCKET_NAME,
            'Key': object_name
        }
        
        if for_download:
            # This header tells the browser to download the file instead of viewing it
            params['ResponseContentDisposition'] = f'attachment; filename="{object_name}"'
            
        url = s3_client.generate_presigned_url(
            'get_object',
            Params=params,
            ExpiresIn=expiration
        )
    except ClientError as e:
        print(f"Error generating presigned URL: {e}")
        return None
    
    return url