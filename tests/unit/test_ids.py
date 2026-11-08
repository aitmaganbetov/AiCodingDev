import uuid

from aicd_platform.ids import uuid7


def test_uuid7_version_variant_and_monotonic() -> None:
    values = [uuid7() for _ in range(5000)]
    assert all(v.version == 7 and v.variant == uuid.RFC_4122 for v in values)
    assert values == sorted(values), "UUIDv7 должны быть монотонны в пределах процесса"
    assert len(set(values)) == len(values)
