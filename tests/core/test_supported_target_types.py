"""Contract tests for the OSS-only diagnostic target types."""

import pytest
from pydantic import ValidationError

from redis_sre_agent.core.clusters import RedisCluster, RedisClusterType
from redis_sre_agent.core.instances import RedisInstance, RedisInstanceType


def _instance(instance_type: str) -> RedisInstance:
    return RedisInstance(
        id="instance-1",
        name="orders",
        connection_url="redis://target.example:6379",
        environment="test",
        usage="cache",
        description="Test target",
        instance_type=instance_type,
    )


def _cluster(cluster_type: str) -> RedisCluster:
    return RedisCluster(
        id="cluster-1",
        name="orders-cluster",
        cluster_type=cluster_type,
        environment="test",
        description="Test cluster",
    )


def test_unknown_instance_type_is_allowed_for_an_unpersisted_target() -> None:
    assert _instance("unknown").instance_type.value == "unknown"


def test_unknown_cluster_type_is_allowed_for_an_unpersisted_draft() -> None:
    assert _cluster("unknown").cluster_type.value == "unknown"


def test_unknown_product_type_is_rejected_by_the_domain_models() -> None:
    with pytest.raises(ValidationError, match="instance_type"):
        _instance("other_vendor")

    with pytest.raises(ValidationError, match="cluster_type"):
        _cluster("other_vendor")


@pytest.mark.parametrize("legacy_type", ["redis_enterprise", "redis_cloud"])
def test_managed_product_types_are_rejected_by_the_domain_models(legacy_type: str) -> None:
    with pytest.raises(ValidationError, match="instance_type"):
        _instance(legacy_type)
    with pytest.raises(ValidationError, match="cluster_type"):
        _cluster(legacy_type)


def test_cluster_is_not_a_diagnostic_connection_endpoint() -> None:
    cluster = _cluster("oss_cluster")

    assert not hasattr(cluster, "connection_url")


def test_domain_models_do_not_expose_managed_platform_fields() -> None:
    managed_fields = {
        "admin_url",
        "admin_username",
        "admin_password",
        "redis_cloud_subscription_id",
        "redis_cloud_database_id",
        "redis_cloud_subscription_type",
        "redis_cloud_database_name",
    }

    assert managed_fields.isdisjoint(RedisInstance.model_fields)
    assert {"admin_url", "admin_username", "admin_password"}.isdisjoint(
        RedisCluster.model_fields
    )
    assert {member.value for member in RedisInstanceType} == {
        "oss_single",
        "oss_cluster",
        "unknown",
    }
    assert {member.value for member in RedisClusterType} == {"oss_cluster", "unknown"}
