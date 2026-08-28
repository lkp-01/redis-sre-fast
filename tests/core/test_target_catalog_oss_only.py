"""Catalog tests for the OSS-only target boundary."""

from redis_sre_agent.core.instances import RedisInstance
from redis_sre_agent.core.targets import build_target_doc_from_instance


def test_instance_catalog_excludes_managed_platform_fields_and_capabilities() -> None:
    instance = RedisInstance(
        id="instance-1",
        name="orders",
        connection_url="redis://target.example:6379",
        environment="test",
        usage="cache",
        description="Test target",
        instance_type="oss_single",
    )

    doc = build_target_doc_from_instance(instance)

    assert "redis_cloud" not in doc.model_dump_json().lower()
    assert doc.capabilities == ["redis", "diagnostics", "metrics", "logs"]
