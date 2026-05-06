import logging
import os
from ftplib import FTP, FTP_TLS, error_perm
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

def connect_ftp():
    host = os.getenv("FTP_HOST", "").strip()
    username = os.getenv("FTP_USERNAME", "").strip()
    password = os.getenv("FTP_PASSWORD", "").strip()
    port = int(os.getenv("FTP_PORT", "21"))
    use_tls = os.getenv("FTP_TLS", "false").lower() == "true"

    if not host or not username or not password:
        raise ValueError("FTP credentials are missing in .env")

    if use_tls:
        ftp = FTP_TLS()
        ftp.connect(host, port, timeout=30)
        ftp.login(username, password)
        ftp.prot_p()
    else:
        ftp = FTP()
        ftp.connect(host, port, timeout=30)
        ftp.login(username, password)

    return ftp

def ensure_remote_dir(ftp, remote_dir: str) -> None:
    try:
        ftp.cwd(remote_dir)
        return
    except error_perm:
        pass

    ftp.mkd(remote_dir)
    ftp.cwd(remote_dir)

def upload_lot_photos_to_inventory_manager(
    local_files: list[Any],
    auction_number: str,
    lot_number: int,
) -> list[str]:
    if not local_files:
        return []

    uploaded_names: list[str] = []
    ftp = connect_ftp()

    try:
        ensure_remote_dir(ftp, str(auction_number))

        if isinstance(local_files[0], tuple):
            files_to_upload = local_files
        else:
            files_to_upload = [(f, f"{lot_number}_{i}.jpg") for i, f in enumerate(sorted(local_files), start=1)]

        for local_file, remote_name in files_to_upload:
            with local_file.open("rb") as f:
                ftp.storbinary(f"STOR {remote_name}", f)
            uploaded_names.append(remote_name)
            logger.info("Uploaded %s as %s/%s", local_file, auction_number, remote_name)

    finally:
        try:
            ftp.quit()
        except Exception:
            pass

    return uploaded_names

def delete_lot_photos_from_inventory_manager(
    auction_number: str,
    remote_names: list[str],
) -> tuple[list[str], list[str]]:
    if not remote_names:
        return [], []

    deleted_names: list[str] = []
    missing_names: list[str] = []
    ftp = connect_ftp()

    try:
        ftp.cwd(str(auction_number))

        for remote_name in remote_names:
            try:
                ftp.delete(remote_name)
                deleted_names.append(remote_name)
                logger.info("Deleted remote file %s/%s", auction_number, remote_name)
            except error_perm as exc:
                if str(exc).startswith("550"):
                    missing_names.append(remote_name)
                    logger.warning("Remote file missing during delete: %s/%s", auction_number, remote_name)
                else:
                    raise
    finally:
        try:
            ftp.quit()
        except Exception:
            pass

    return deleted_names, missing_names