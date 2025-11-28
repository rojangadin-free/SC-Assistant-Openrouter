# download_model.py

print("---------------------------------------------------------")
print("--- [Pre-flight] Starting Hugging Face model download ---")
print("---------------------------------------------------------")

# We import the function directly from the helper module.
from src.helper import get_local_embeddings

# Calling this function will trigger the download from Hugging Face
# and save the model to the local cache on the EC2 instance.
# Subsequent calls to this function in the main app will be instant.
get_local_embeddings()

print("-------------------------------------------------------")
print("--- [Pre-flight] Hugging Face model download complete ---")
print("-------------------------------------------------------")