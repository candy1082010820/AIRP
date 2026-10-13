import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(default_factory=lambda: Path("data/airp.sqlite3"))
    admin_key: str = ""
    read_key: str = ""
    public_url: str = ""

    def __post_init__(self):
        object.__setattr__(self, "db_path", Path(self.db_path))
        if bool(self.admin_key) != bool(self.read_key):
            raise ValueError("Set both AIRP_ADMIN_KEY and AIRP_READ_KEY, or neither for loopback-only development.")
        if self.admin_key and self.admin_key == self.read_key:
            raise ValueError("AIRP_ADMIN_KEY and AIRP_READ_KEY must be different.")
        if self.public_url:
            url = urlsplit(self.public_url)
            if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("AIRP_PUBLIC_URL must be an HTTPS base URL without credentials, query or fragment.")
            if not self.admin_key or not self.read_key:
                raise ValueError("Public deployments require both AIRP_ADMIN_KEY and AIRP_READ_KEY.")
            object.__setattr__(self, "public_url", self.public_url.rstrip("/"))

    @classmethod
    def from_env(cls):
        return cls(
            db_path=Path(os.environ.get("AIRP_DB_PATH", "data/airp.sqlite3")),
            admin_key=os.environ.get("AIRP_ADMIN_KEY", ""),
            read_key=os.environ.get("AIRP_READ_KEY", ""),
            public_url=os.environ.get("AIRP_PUBLIC_URL", ""),
        )
