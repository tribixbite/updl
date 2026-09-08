from typing import Optional


class Config:
    def __init__(self) -> None:
        pass

    ADDRESS: str = "unifi"
    PORT: int = 443
    PROTOCOL: str = "https"
    USERNAME: str = "ubnt"
    PASSWORD: Optional[str] = None
    VERIFY_SSL: bool = False
    USE_UNSAFE_COOKIE_JAR: bool = False
    DESTINATION_PATH: str = "./"
    USE_SUBFOLDERS: bool = False
    TOUCH_FILES: bool = False
    SKIP_EXISTING_FILES: bool = False
    IGNORE_FAILED_DOWNLOADS: bool = False
    DISABLE_ALIGNMENT: bool = False
    DISABLE_SPLITTING: bool = False
    DOWNLOAD_WAIT: int = 0
    DOWNLOAD_TIMEOUT: float = (
        60.0  # aka read_timeout - time to wait until a socket read response happens
    )
    MAX_RETRIES: int = 3
    USE_UTC_FILENAMES: bool = False

    # Cache the UniFi OS session token between runs. Accounts backed by Ubiquiti SSO
    # need a second factor at login, and /api/auth/login is rate limited, so reusing a
    # live token is what keeps repeat runs from demanding a freshly emailed code.
    USE_SESSION_STORE: bool = True

    # How hard to work to prove a file already on disk is intact before skipping it.
    # See protect_archiver.verify for what each level checks.
    VERIFY_LEVEL: str = "quick"
