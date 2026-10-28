"""Render the small streaming-aware proxy configuration used during rollout."""

import argparse
import re


def render(backend, drain=180):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,80}", backend):
        raise ValueError("Invalid backend container name")
    return f"""# sc-active:{backend}
worker_processes auto;
worker_shutdown_timeout {int(drain)}s;
events {{ worker_connections 1024; }}
http {{
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    access_log /dev/stdout;
    error_log /dev/stderr;
    client_max_body_size 64m;
    upstream assistant {{ server {backend}:8080; }}
    server {{
        listen 8080;
        add_header X-SC-Backend {backend} always;
        location / {{
            proxy_pass http://assistant;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
            proxy_buffering off;
            # Nginx hides X-Accel-* headers by default. Preserve this one so
            # the HTTPS proxy outside this container also disables buffering.
            proxy_pass_header X-Accel-Buffering;
            proxy_set_header Accept-Encoding "";
            proxy_request_buffering off;
            proxy_cache off;
            gzip off;
            proxy_read_timeout 180s;
            proxy_send_timeout 180s;
        }}
    }}
}}
"""


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("backend")
    args = parser.parse_args()
    print(render(args.backend), end="")
