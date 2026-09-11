# syntax=docker/dockerfile:1

# python:3.10-slim-bookworm, not slim-buster.
#
# Debian 10 "buster" is end-of-life and its packages have moved to archive
# mirrors, so `apt-get install` inside a buster image now fails on a 404. That
# matters here because the previous build broke while trying to COMPILE lxml,
# and the standard fix for that ("just apt-get install libxml2-dev") is exactly
# what a buster base can no longer do. Bookworm keeps that escape hatch open.
#
# The real fix is in requirements.txt: every C-extension package is pinned to a
# version with a cp310 manylinux wheel, so nothing needs a compiler at all.
FROM python:3.10-slim-bookworm

# Unbuffered output, or the first crash in the container logs nothing useful.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Requirements first, so editing application code does not invalidate the layer
# that installs ~120 packages (including torch).
COPY requirements.txt .

# --only-binary=:all: turns "would need to compile from source" into an
# immediate, clearly-named failure instead of a slow build that dies further
# down in a C compiler error. If this line fails, the message names the package
# whose pin needs fixing.
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt

# BM25 tokenizer data for pinecone-text. nltk is now a declared dependency
# rather than a transitive accident, so this can be relied on.
RUN python3 -m nltk.downloader -d /usr/local/share/nltk_data \
        punkt punkt_tab averaged_perceptron_tagger_eng
ENV NLTK_DATA=/usr/local/share/nltk_data

# Bake the embedding + reranker models into the image. Downloading them at
# startup instead would make the first request after every deploy slow enough to
# look like a hang, and would fail outright if HuggingFace is unreachable.
COPY download_model.py .
RUN python3 download_model.py

COPY . /app

# Fail the build if any declared dependency is actually missing. Without this, a
# package that is imported but not listed ships a green pipeline and a container
# that crash-loops on EC2 — the pipeline says success, the site is down, and
# nothing connects the two.
#
# This imports the LIBRARIES, not the app. Importing sc_assistant would be a
# stronger check but cannot run here: rag/chain.py opens a Pinecone client at
# module level and config.py raises without PINECONE_API_KEY, which is a runtime
# secret and is deliberately not available at build time. A build-time check that
# needs production credentials is a check that gets deleted the first time it
# fails, so this stays at the dependency level, which is the bug class that broke
# this deploy.
RUN python3 -c "import flask, gunicorn, langchain, langchain_core, langchain_community, langchain_classic, langchain_openai, langchain_pinecone, langchain_huggingface, langgraph, pinecone, pinecone_text, nltk, sentence_transformers, lxml, fitz, pdfplumber, docx, PIL, boto3, jose, cryptography, dotenv; print('all declared dependencies import OK')"


# Thread pinning for the cross-encoder. torch otherwise starts one compute
# thread per core in EVERY worker, so on the 2-vCPU target box two workers ask
# for 4 threads on 2 cores and lose time to context switching. Set here as well
# as in rag/reranker.py because these must be read before torch is imported, and
# an env var in the image is the only place guaranteed to precede that.
#
# See docs/CAPACITY.md for the measurements behind this and the numbers below.
ENV OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    TOKENIZERS_PARALLELISM=false

EXPOSE 8080

# 2 workers x 4 threads on a small EC2 box (m7i-flex.large: 2 vCPU, 8 GB). Each
# worker loads its own copy of the reranker model — ~1.3-1.6 GB resident — so
# this is a MEMORY ceiling as much as a CPU one: 2 workers fit comfortably in
# 8 GB, 4 would risk the OOM killer taking a worker mid-answer.
#
# The threads are not there to add CPU capacity (there is none to add on 2 cores)
# but because most of a request is spent WAITING — on Pinecone, on DynamoDB, and
# above all on the LLM gateway streaming tokens back. A thread parked on a socket
# costs nothing, and without them a handful of slow answers would occupy both
# workers and queue everyone else at the front door. The genuinely CPU-bound
# phase is bounded separately by RERANKER_CONCURRENCY.
#
# --timeout 120 covers a slow first LLM response.
#
# --worker-tmp-dir /dev/shm: gunicorn heartbeats through a temp file, and on an
# EBS-backed instance /tmp can stall long enough under load for the arbiter to
# decide a busy-but-healthy worker has hung and kill it mid-answer. /dev/shm is
# memory, so the heartbeat cannot be delayed by disk.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", \
     "--worker-tmp-dir", "/dev/shm", \
     "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-", \
     "run:app"]


