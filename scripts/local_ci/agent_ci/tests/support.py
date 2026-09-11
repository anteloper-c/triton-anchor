"""Small file/Git/attachment fixtures shared by Worker integration tests."""

import gzip
import hashlib
import subprocess


def git(directory, *args, input_data=None):
    return (
        subprocess.check_output(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                *args,
            ],
            cwd=directory,
            input=input_data,
            stderr=subprocess.DEVNULL,
        )
        .decode()
        .strip()
    )


class ReleaseBoundary:
    def __init__(self):
        self.offline = False
        self.fail_optional = False
        self.optional_attempts = 0
        self.rows, self.data = [], {}

    def release(self, *args):
        if self.offline:
            raise OSError("simulated Gitee attachment outage")
        return {"id": 1}

    def attachments(self, release_id):
        return list(self.rows)

    def upload(self, release_id, name, path):
        if gzip.decompress(path.read_bytes()).startswith(b"OPTIONAL-FIXTURE"):
            self.optional_attempts += 1
            if self.fail_optional:
                raise OSError("simulated optional attachment outage")
        row = {
            "id": len(self.rows) + 1,
            "name": name,
            "browser_download_url": "https://gitee.com/test/ci/attachment",
        }
        self.rows.append(row)
        self.data[row["id"]] = path.read_bytes()
        return row

    def verified(self, release_id, attachment, expected):
        raw = self.data[attachment["id"]]
        return (
            len(raw) == expected["size"]
            and hashlib.sha256(raw).hexdigest() == expected["sha256"]
        )
