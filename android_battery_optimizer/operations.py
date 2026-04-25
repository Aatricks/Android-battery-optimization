STANDBY_BUCKET_MAP = {
    "active": "10",
    "working_set": "20",
    "frequent": "30",
    "rare": "40",
    "restricted": "45",
    "never": "50",
}

def is_restorable_bucket(bucket: str) -> bool:
    try:
        normalize_restorable_bucket(bucket)
        return True
    except ValueError:
        return False

def normalize_restorable_bucket(bucket: str) -> str:
    if bucket is None:
        raise ValueError("Standby bucket cannot be None")
    bucket_str = str(bucket).strip().lower()
    if bucket_str in STANDBY_BUCKET_MAP:
        return bucket_str
    for name, num in STANDBY_BUCKET_MAP.items():
        if bucket_str == num:
            return name
    raise ValueError(f"Non-restorable standby bucket: {bucket}")

