"""Tests for pipeline.tile_upload: the build's tiles to the bucket, through a fake S3 client."""

import json
from pathlib import Path

import pytest

from pipeline.common import sha256_of
from pipeline.tests.place_geometry_fixtures import run, with_lake
from pipeline.tile_upload import UploadFailed, upload

CONTENT_TYPE = "application/x-ndjson"


class FakeS3:
    """The three S3 calls the upload makes, kept in a dict. Never reaches the network.

    Lists PAGE keys at a time, so every listing of more than that is paginated.
    """

    PAGE = 2

    def __init__(self, objects=None, fail_put=(), fail_delete=()):
        self.objects = dict(objects or {})
        self.fail_put = set(fail_put)
        self.fail_delete = set(fail_delete)
        self.puts = []
        self.delete_batches = []
        self.list_calls = 0

    def list_objects_v2(self, *, Bucket, ContinuationToken=None):
        assert Bucket == "bucket"
        self.list_calls += 1
        keys = sorted(self.objects)
        start = int(ContinuationToken or 0)
        page = keys[start : start + self.PAGE]
        answer = {"KeyCount": len(page), "IsTruncated": start + self.PAGE < len(keys)}
        if page:
            answer["Contents"] = [{"Key": key} for key in page]
        if answer["IsTruncated"]:
            answer["NextContinuationToken"] = str(start + self.PAGE)
        return answer

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


def build_dir(tmp_path: Path, tiles: dict[str, bytes], put: list[str]) -> Path:
    """A build of these tiles. put is changed.json's list, the tiles that differ from the previous list."""
    out = tmp_path / "build"
    out.mkdir()
    listing = {}
    for key, data in tiles.items():
        path = out / "tiles" / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        listing[key] = {"sha256": sha256_of(data), "bytes": len(data)}
    (out / "tiles.json").write_text(json.dumps({"tiles": listing}))
    (out / "changed.json").write_text(json.dumps({"put": put, "delete": []}))
    return out


def held(build: Path) -> dict[str, tuple[bytes, str]]:
    """What the bucket holds once a build is uploaded whole: every tile, with its bytes."""
    root = build / "tiles"
    if not root.is_dir():
        return {}
    return {p.relative_to(root).as_posix(): (p.read_bytes(), CONTENT_TYPE) for p in root.rglob("*.ndjson")}


def test_puts_exactly_the_changed_tiles_with_their_bytes(tmp_path):
    out = build_dir(
        tmp_path,
        {"water/-331/1513.ndjson": b'{"water":"sea"}\n', "roads/-331/1513.ndjson": b'{"road":1}\n'},
        put=["water/-331/1513.ndjson"],
    )
    s3 = FakeS3({"roads/-331/1513.ndjson": (b'{"road":1}\n', CONTENT_TYPE)})
    result = upload(out, s3, "bucket")
    assert s3.puts == ["water/-331/1513.ndjson"]
    assert s3.objects == held(out)
    assert result == {"put": 1, "delete": 0}


def test_a_tile_the_bucket_lacks_is_put_though_it_did_not_change(tmp_path):
    out = build_dir(tmp_path, {"water/1/1.ndjson": b"a\n", "water/1/2.ndjson": b"b\n"}, put=[])
    s3 = FakeS3({"water/1/1.ndjson": (b"a\n", CONTENT_TYPE)})
    assert upload(out, s3, "bucket") == {"put": 1, "delete": 0}
    assert s3.puts == ["water/1/2.ndjson"]
    assert s3.objects == held(out)


def test_deletes_every_tile_in_the_bucket_the_build_does_not_have(tmp_path):
    out = build_dir(tmp_path, {"roads/-300/1401.ndjson": b"y\n"}, put=[])
    s3 = FakeS3({
        "roads/-300/1400.ndjson": (b"x", CONTENT_TYPE),
        "roads/-300/1401.ndjson": (b"y\n", CONTENT_TYPE),
        "water/-300/1400.ndjson": (b"z", CONTENT_TYPE),
    })
    result = upload(out, s3, "bucket")
    assert set(s3.objects) == {"roads/-300/1401.ndjson"}
    assert result == {"put": 0, "delete": 2}


def test_a_key_in_the_bucket_that_is_not_a_tile_is_left_alone(tmp_path):
    out = build_dir(tmp_path, {}, put=[])
    s3 = FakeS3({"README.txt": (b"keep", ""), "water/1/1.json": (b"keep", "")})
    assert upload(out, s3, "bucket") == {"put": 0, "delete": 0}
    assert set(s3.objects) == {"README.txt", "water/1/1.json"}


def test_the_bucket_listing_is_read_page_by_page(tmp_path):
    out = build_dir(tmp_path, {}, put=[])
    s3 = FakeS3({f"water/-300/{col}.ndjson": (b"x", CONTENT_TYPE) for col in range(7)})
    assert upload(out, s3, "bucket") == {"put": 0, "delete": 7}
    assert s3.list_calls == 4
    assert s3.objects == {}


def test_deletes_go_in_batches_of_at_most_a_thousand(tmp_path, monkeypatch):
    monkeypatch.setattr(FakeS3, "PAGE", 1000)
    keys = sorted(f"water/-300/{col}.ndjson" for col in range(2500))
    out = build_dir(tmp_path, {}, put=[])
    s3 = FakeS3({key: (b"x", CONTENT_TYPE) for key in keys})
    upload(out, s3, "bucket")
    assert [len(batch) for batch in s3.delete_batches] == [1000, 1000, 500]
    assert [key for batch in s3.delete_batches for key in batch] == keys


def test_nothing_changed_makes_no_puts_or_deletes(tmp_path):
    out = build_dir(tmp_path, {"water/1/1.ndjson": b"x\n"}, put=[])
    s3 = FakeS3(held(out))
    assert upload(out, s3, "bucket") == {"put": 0, "delete": 0}
    assert s3.puts == [] and s3.delete_batches == []


def test_a_failed_put_fails_the_upload_and_deletes_nothing(tmp_path):
    out = build_dir(
        tmp_path,
        {"water/1/1.ndjson": b"a\n", "water/1/2.ndjson": b"b\n"},
        put=["water/1/1.ndjson", "water/1/2.ndjson"],
    )
    s3 = FakeS3({"water/9/9.ndjson": (b"old", "")}, fail_put={"water/1/2.ndjson"})
    with pytest.raises(UploadFailed, match="water/1/2.ndjson"):
        upload(out, s3, "bucket")
    assert s3.delete_batches == []
    assert "water/9/9.ndjson" in s3.objects


def test_a_delete_the_bucket_refuses_fails_the_upload(tmp_path):
    out = build_dir(tmp_path, {}, put=[])
    s3 = FakeS3({"water/9/9.ndjson": (b"old", "")}, fail_delete={"water/9/9.ndjson"})
    with pytest.raises(UploadFailed, match="water/9/9.ndjson"):
        upload(out, s3, "bucket")


@pytest.mark.parametrize("key", ["../secret", "water/1/1.json", "/water/1/1.ndjson", "other/1/1.ndjson"])
def test_a_key_that_is_not_a_tile_is_refused_before_any_call(tmp_path, key):
    out = build_dir(tmp_path, {"water/1/1.ndjson": b"a\n"}, put=["water/1/1.ndjson", key])
    s3 = FakeS3()
    with pytest.raises(UploadFailed, match="not a tile key"):
        upload(out, s3, "bucket")
    assert s3.puts == [] and s3.list_calls == 0


def test_a_run_that_failed_half_way_is_repaired_by_the_next(tmp_path):
    # The release's tiles.json lists build a, and the bucket holds it.
    a = run(tmp_path, name="a")
    s3 = FakeS3(held(a))
    # Build b moves the first lake and adds a second one in a new tile. Its
    # upload puts the new tile, then the bucket refuses the moved lake's, so
    # the run fails and the release keeps a's tiles.json.
    osm = tmp_path / "b.osm"
    osm.write_text(
        with_lake("Second Lake").replace('<node id="3" version="1" lat="-33.248"', '<node id="3" version="2" lat="-33.2475"')
    )
    b = run(tmp_path, osm=osm, previous=a / "tiles.json", name="b")
    s3.fail_put = {"water/-333/1511.ndjson"}
    with pytest.raises(UploadFailed, match="water/-333/1511.ndjson"):
        upload(b, s3, "bucket")
    assert "water/-332/1512.ndjson" in s3.objects
    # The next week's build c is a again. Diffed against a's tiles.json it has
    # nothing to put or delete, but the second lake's tile is in the bucket.
    s3.fail_put = set()
    c = run(tmp_path, previous=a / "tiles.json", name="c")
    assert json.loads((c / "changed.json").read_text()) == {"put": [], "delete": []}
    assert upload(c, s3, "bucket") == {"put": 0, "delete": 1}
    assert s3.objects == held(c)
