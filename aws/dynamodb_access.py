"""Thread-local DynamoDB resources with bounded connection/retry timeouts."""

import threading

_local = threading.local()


def get_table(name):
    if not hasattr(_local, "resource"):
        import boto3.session
        from botocore.config import Config

        from config import AWS_REGION

        _local.resource = boto3.session.Session().resource(
            "dynamodb", region_name=AWS_REGION,
            config=Config(connect_timeout=3, read_timeout=5, max_pool_connections=2,
                          retries={"mode": "standard", "total_max_attempts": 2}),
        )
        _local.tables = {}
    if name not in _local.tables:
        _local.tables[name] = _local.resource.Table(name)
    return _local.tables[name]


def reset_tables():
    global _local
    _local = threading.local()


class ThreadLocalTable:
    """Keep existing Table call sites while never sharing a Boto3 resource."""

    def __init__(self, name):
        self.name = name

    def __getattr__(self, attribute):
        return getattr(get_table(self.name), attribute)
