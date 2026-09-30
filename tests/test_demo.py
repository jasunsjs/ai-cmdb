from cmdb.demo import encode_multipart


def test_encode_multipart() -> None:
    body, content_type = encode_multipart("file", "okta.json", b'[{"a": 1}]')

    boundary = content_type.split("boundary=")[1]
    assert content_type.startswith("multipart/form-data; boundary=")
    assert body.startswith(f"--{boundary}\r\n".encode())
    assert b'name="file"; filename="okta.json"' in body
    assert b'\r\n\r\n[{"a": 1}]\r\n' in body
    assert body.endswith(f"--{boundary}--\r\n".encode())
