import os
from ftplib import FTP


def rename_jpgs_with_spaces() -> None:
    host = os.getenv("FTP_HOST", "").strip()
    username = os.getenv("FTP_USERNAME", "").strip()
    password = os.getenv("FTP_PASSWORD", "")
    remote_dir = os.getenv("FTP_RENAME_DIR", "").strip()

    if not host or not username or not password:
        raise RuntimeError("Set FTP_HOST, FTP_USERNAME, and FTP_PASSWORD before running.")

    with FTP(host) as ftp:
        ftp.login(username, password)
        if remote_dir:
            ftp.cwd(remote_dir)

        print(ftp.pwd())
        for filename in ftp.nlst():
            if not filename.lower().endswith(".jpg"):
                continue

            new_name = filename.replace(" ", "_")
            if new_name != filename:
                ftp.rename(filename, new_name)
                print(f"Renamed: {filename} -> {new_name}")


if __name__ == "__main__":
    rename_jpgs_with_spaces()
