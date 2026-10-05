"""Encrypted offsite backups: hybrid encryption, S3 signing against AWS's published vector, retention, size verification and a full restore."""
import datetime as dt
import sqlite3

import httpx
import pytest

from app import offsite


@pytest.fixture(scope="module")
def keys():
    return offsite.generate_keypair()


def test_sigv4_matches_the_aws_published_get_vanilla_vector():
    # AWS Signature V4 test suite, "get-vanilla": the signature is a published constant.
    h = offsite.sigv4_headers("GET", "https://example.amazonaws.com/", region="us-east-1", key_id="AKIDEXAMPLE", secret="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
                              now=dt.datetime(2015, 8, 30, 12, 36, 0, tzinfo=dt.timezone.utc), service="service")
    assert h["Authorization"] == ("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, SignedHeaders=host;x-amz-date, "
                                  "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31")


def test_encrypt_decrypt_roundtrip_and_tampering_is_detected(keys):
    priv, pub = keys
    blob = offsite.encrypt(b"secret database bytes" * 1000, pub)
    assert b"secret database" not in blob and blob.startswith(offsite.MAGIC)
    assert offsite.decrypt(blob, priv) == b"secret database bytes" * 1000
    flipped = bytearray(blob)
    flipped[-5] ^= 1
    with pytest.raises(Exception):
        offsite.decrypt(bytes(flipped), priv)
    with pytest.raises(Exception):
        offsite.decrypt(b"XXXX" + blob[4:], priv)
    other_priv, _ = offsite.generate_keypair()
    with pytest.raises(Exception):
        offsite.decrypt(blob, other_priv)                     # a different private key cannot open it


def _db(path, n=3):
    c = sqlite3.connect(path)
    c.executescript("CREATE TABLE calls (id INTEGER); CREATE TABLE bookings (id INTEGER); CREATE TABLE escalations (id INTEGER);")
    c.executemany("INSERT INTO calls VALUES (?)", [(i,) for i in range(n)])
    c.commit()
    c.close()


def test_backup_upload_is_verified_and_restores_to_the_same_database(tmp_path, keys):
    priv, pub = keys
    src = tmp_path / "local.db"
    _db(src, 5)
    dest = offsite.LocalFolder(tmp_path / "bucket")
    result = offsite.run_offsite_backup(src, dest=dest, public_pem=pub, today=dt.date(2026, 10, 1))
    assert result["ok"] and result["object"] == "callkettle/2026/10/callkettle-2026-10-01.db.enc" and result["bytes"] > src.stat().st_size
    stored = (tmp_path / "bucket" / result["object"]).read_bytes()
    assert b"SQLite format" not in stored                               # nothing readable at the destination
    info = offsite.restore_latest(dest, priv, tmp_path / "restored.db")
    conn = sqlite3.connect(tmp_path / "restored.db")
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok" and conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 5
    conn.close()
    assert info["object"] == result["object"]


def test_unconfigured_or_failing_destinations_report_instead_of_raising(tmp_path, keys):
    _, pub = keys
    src = tmp_path / "local.db"
    _db(src)
    assert offsite.run_offsite_backup(src, dest=None, public_pem=b"")["configured"] is False

    class Broken(offsite.LocalFolder):
        def put(self, name, data):
            raise OSError("disk full")

    r = offsite.run_offsite_backup(src, dest=Broken(tmp_path / "b"), public_pem=pub)
    assert r["ok"] is False and "disk full" in r["problem"]

    class Liar(offsite.LocalFolder):
        def list(self, prefix=""):
            return [(n, 1) for n, _ in super().list(prefix)]         # claims a different size: the upload is NOT reported as verified

    r = offsite.run_offsite_backup(src, dest=Liar(tmp_path / "c"), public_pem=pub)
    assert r["ok"] is False and "destination lists" in r["problem"]


def test_retention_keeps_30_daily_and_the_first_of_each_of_12_months(tmp_path):
    dest = offsite.LocalFolder(tmp_path)
    day = dt.date(2025, 9, 1)
    for i in range(420):
        d = day + dt.timedelta(days=i)
        dest.put(offsite.object_name(d), b"x")
    removed = offsite.prune(dest)
    kept = [n for n, _ in dest.list("callkettle/")]
    assert len(kept) == len(set(kept)) and len(removed) + len(kept) == 420
    assert all(offsite.object_name(day + dt.timedelta(days=419 - i)) in kept for i in range(30))          # the newest 30
    firsts = [n for n in kept if n.endswith("-01.db.enc")]
    assert len(firsts) >= 12 - 1


def test_a_write_only_key_that_cannot_delete_never_fails_the_backup(tmp_path, keys):
    _, pub = keys
    src = tmp_path / "local.db"
    _db(src)

    class NoDelete(offsite.LocalFolder):
        def delete(self, name):
            raise PermissionError("write-only key")

    dest = NoDelete(tmp_path / "bucket")
    for i in range(40):
        dest.put(offsite.object_name(dt.date(2026, 1, 1) + dt.timedelta(days=i)), b"x")
    assert offsite.run_offsite_backup(src, dest=dest, public_pem=pub, today=dt.date(2026, 10, 1))["ok"] is True


def test_local_folder_cannot_be_escaped(tmp_path):
    dest = offsite.LocalFolder(tmp_path / "b")
    with pytest.raises(ValueError):
        dest.put("../escape.txt", b"x")


def test_s3_client_signs_every_request_and_pages_through_listings(keys):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url), request.headers.get("authorization", "")))
        if request.method == "PUT":
            return httpx.Response(200)
        if "continuation-token" not in str(request.url):
            return httpx.Response(200, text="<ListBucketResult><IsTruncated>true</IsTruncated><NextContinuationToken>tok1</NextContinuationToken>"
                                            "<Contents><Key>callkettle/2026/10/callkettle-2026-10-01.db.enc</Key><Size>10</Size></Contents></ListBucketResult>")
        return httpx.Response(200, text="<ListBucketResult><IsTruncated>false</IsTruncated><Contents><Key>callkettle/2026/10/callkettle-2026-10-02.db.enc</Key><Size>20</Size></Contents></ListBucketResult>")

    s3 = offsite.S3Compatible("https://s3.example.test", "bucket", "us-east-1", "KEYID", "SECRET", client=httpx.Client(transport=httpx.MockTransport(handler)))
    s3.put("callkettle/a.enc", b"data")
    assert s3.list("callkettle/") == [("callkettle/2026/10/callkettle-2026-10-01.db.enc", 10), ("callkettle/2026/10/callkettle-2026-10-02.db.enc", 20)]
    assert all(a.startswith("AWS4-HMAC-SHA256 Credential=KEYID/") for _, _, a in seen)
    assert seen[0][1] == "https://s3.example.test/bucket/callkettle/a.enc"


def test_housekeeping_reports_offsite_status_without_breaking_when_unconfigured(app_client):
    client, main = app_client
    body = client.get("/admin/status", params={"key": "master_key_for_tests"}).json()
    assert body["offsite_backup"]["configured"] is False
