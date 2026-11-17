"""Tests for pipeline.tile_upload: changed tiles to the bucket, through a fake S3 client."""

import json
from pathlib import Path

import pytest

from pipeline.tile_upload import UploadFailed, upload


class FakeS3:
    """The two S3 calls the upload makes, kept in a dict. Never reaches the network."""

    def __init__(self, objects=None, fail_put=(), fail_delete=()):
        self.objects = dict(objects or {})
        self.fail_put = set(fail_put)
        self.fail_delete = set(fail_delete)
        self.puts = []
        self.delete_batches = []

    def put_object(self, *, Bucket, Key, Body, ContentType):
        assert Bucket == "bucket"
        if Key in self.fail_put:
            raise RuntimeError(f"refused {Key}")
        self.puts.append(Key)
        self.objects[Key] = (Body, ContentType)
        return {}

    def delete_objects(self, *, Bucket, Delete):
        assert Bucket == "bucket"
        keys = [item["Key"] for item in Delete["Objects"]]
        self.delete_batches.append(keys)
        errors = []
        for key in keys:
            if key in self.fail_delete:
                errors.append({"Key": key, "Code": "InternalError", "Message": "no"})
            else:
                self.objects.pop(key, None)
        return {"Errors": errors} if errors else {}


def build_dir(tmp_path: Path, tiles: dict[str, bytes], put: list[str], delete: list[str]) -> Path:
    out = tmp_path / "build"
    out.mkdir()
    for key, data in tiles.items():
        path = out / "tiles" / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (out / "changed.json").write_text(json.dumps({"put": put, "delete": delete}))
    return out


def test_puts_exactly_the_changed_tiles_with_their_bytes(tmp_path):
    out = build_dir(
        tmp_path,
        {"water/-331/1513.ndjson": b'{"water":"sea"}\n', "roads/-331/1513.ndjson": b'{"road":1}\n'},
        put=["water/-331/1513.ndjson"],
        delete=[],
    )
    s3 = FakeS3()
    result = upload(out, s3, "bucket")
    assert s3.objects == {"water/-331/1513.ndjson": (b'{"water":"sea"}\n', "application/x-ndjson")}
    assert result == {"put": 1, "delete": 0}


def test_deletes_the_tiles_that_no_longer_exist(tmp_path):
    out = build_dir(tmp_path, {}, put=[], delete=["roads/-300/1400.ndjson"])
    s3 = FakeS3({"roads/-300/1400.ndjson": (b"x", "application/x-ndjson"), "roads/-300/1401.ndjson": (b"y", "")})
    result = upload(out, s3, "bucket")
    assert set(s3.objects) == {"roads/-300/1401.ndjson"}
    assert result == {"put": 0, "delete": 1}


def test_deletes_go_in_batches_of_at_most_a_thousand(tmp_path):
    keys = [f"water/-300/{col}.ndjson" for col in range(2500)]
    out = build_dir(tmp_path, {}, put=[], delete=keys)
    s3 = FakeS3()
    upload(out, s3, "bucket")
    assert [len(batch) for batch in s3.delete_batches] == [1000, 1000, 500]
    assert [key for batch in s3.delete_batches for key in batch] == keys


def test_nothing_changed_makes_no_calls(tmp_path):
    out = build_dir(tmp_path, {"water/1/1.ndjson": b"x\n"}, put=[], delete=[])
    s3 = FakeS3()
    assert upload(out, s3, "bucket") == {"put": 0, "delete": 0}
    assert s3.puts == [] and s3.delete_batches == []


def test_a_failed_put_fails_the_upload_and_deletes_nothing(tmp_path):
    out = build_dir(
        tmp_path,
        {"water/1/1.ndjson": b"a\n", "water/1/2.ndjson": b"b\n"},
        put=["water/1/1.ndjson", "water/1/2.ndjson"],
        delete=["water/9/9.ndjson"],
    )
    s3 = FakeS3({"water/9/9.ndjson": (b"old", "")}, fail_put={"water/1/2.ndjson"})
    with pytest.raises(UploadFailed, match="water/1/2.ndjson"):
        upload(out, s3, "bucket")
    assert s3.delete_batches == []
    assert "water/9/9.ndjson" in s3.objects


def test_a_delete_the_bucket_refuses_fails_the_upload(tmp_path):
    out = build_dir(tmp_path, {}, put=[], delete=["water/9/9.ndjson"])
    s3 = FakeS3(fail_delete={"water/9/9.ndjson"})
    with pytest.raises(UploadFailed, match="water/9/9.ndjson"):
        upload(out, s3, "bucket")


@pytest.mark.parametrize("key", ["../secret", "water/1/1.json", "/water/1/1.ndjson", "other/1/1.ndjson"])
def test_a_key_that_is_not_a_tile_is_refused_before_any_call(tmp_path, key):
    out = build_dir(tmp_path, {"water/1/1.ndjson": b"a\n"}, put=["water/1/1.ndjson", key], delete=[])
    s3 = FakeS3()
    with pytest.raises(UploadFailed, match="not a tile key"):
        upload(out, s3, "bucket")
    assert s3.puts == []
