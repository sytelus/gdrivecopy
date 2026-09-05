"""Deterministic in-memory Drive server for end-to-end offline job tests."""

import hashlib

import requests

from gdrivecopy.drive import FOLDER_MIME, DriveApiError, UploadResponse, UploadStatus


class FakeDrive:
    def __init__(self):
        self.items = {
            "root": {"id": "root", "name": "Root", "mimeType": FOLDER_MIME, "parents": []}
        }
        self.content = {}
        self.sessions = {}
        self.changes = []
        self.control = None
        self.next_id = 0
        self.page_calls = 0
        self.download_calls = []
        self.upload_calls = 0
        self.cancel_after_range = False
        self.cancel_after_chunk = False
        self.lose_multipart_response = False

    def add_file(self, name, content, parent="root", identity=None):
        self.next_id += 1
        identity = identity or f"file-{self.next_id}"
        self.content[identity] = content
        self.items[identity] = {
            "id": identity,
            "name": name,
            "mimeType": "application/octet-stream",
            "size": str(len(content)),
            "md5Checksum": hashlib.md5(content).hexdigest(),
            "version": "1",
            "modifiedTime": "2026-09-04T12:00:00Z",
            "parents": [parent],
            "capabilities": {"canDownload": True},
        }
        self.changes.append({"fileId": identity, "file": dict(self.items[identity])})
        return identity

    def file_metadata(self, identity):
        if identity not in self.items:
            raise DriveApiError(404, "not found")
        return dict(self.items[identity])

    def folder_page(self, identity, token=None):
        self.page_calls += 1
        items = [
            dict(item)
            for item in self.items.values()
            if identity in item.get("parents", []) and not item.get("trashed")
        ]
        return {"files": items}

    def change_token(self):
        return str(len(self.changes))

    def change_page(self, token):
        return {"changes": self.changes[int(token) :], "newStartPageToken": str(len(self.changes))}

    def generate_ids(self, count=100):
        result = []
        for _ in range(count):
            self.next_id += 1
            result.append(f"reserved-{self.next_id}")
        return result

    def create_folder(self, name, parent_id):
        identity = self.generate_ids(1)[0]
        self.items[identity] = {
            "id": identity,
            "name": name,
            "mimeType": FOLDER_MIME,
            "parents": [parent_id],
        }
        self.changes.append({"fileId": identity, "file": dict(self.items[identity])})
        return identity

    def multipart_upload(self, file_path, name, parent_id, file_id=None, **_kwargs):
        self.upload_calls += 1
        if file_id in self.items:
            return self.upload_result(file_id)
        identity = self.add_file(name, file_path.read_bytes(), parent_id, file_id)
        if self.lose_multipart_response:
            self.lose_multipart_response = False
            raise requests.Timeout("response lost after server commit")
        return self.upload_result(identity)

    def upload_result(self, identity):
        return UploadResponse(identity, self.items[identity]["md5Checksum"])

    def initiate_resumable_upload(self, name, parent_id, file_size, file_id=None, **_kwargs):
        if file_id in self.items:
            raise DriveApiError(409, "already created")
        uri = f"https://www.googleapis.com/upload/drive/v3/files?upload_id={file_id}"
        self.sessions[uri] = {
            "name": name,
            "parent": parent_id,
            "id": file_id,
            "size": file_size,
            "data": bytearray(),
            "completed": None,
        }
        return uri

    def upload_chunk(self, session_uri, data, start, total):
        session = self.sessions[session_uri]
        assert len(session["data"]) == start
        session["data"].extend(data)
        if self.cancel_after_chunk:
            self.cancel_after_chunk = False
            self.control.cancel()
        if len(session["data"]) == total:
            self.add_file(session["name"], bytes(session["data"]), session["parent"], session["id"])
            session["completed"] = self.upload_result(session["id"])
            return session["completed"]
        return None

    def query_upload_status(self, uri, total):
        session = self.sessions[uri]
        return UploadStatus(len(session["data"]), session["completed"])

    def download_range(self, identity, start, length, total):
        self.download_calls.append((identity, start, length))
        if self.cancel_after_range:
            self.cancel_after_range = False
            self.control.cancel()
        return self.content[identity][start : start + length]

    def refresh_credentials(self):
        pass

    def export_document(self, identity, mime_type):
        return self.content[identity]

    def trash_file(self, identity):
        self.items[identity]["trashed"] = True
        self.changes.append({"fileId": identity, "removed": True})
