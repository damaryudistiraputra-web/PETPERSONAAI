"""Run once per day using your hosting provider's scheduler/cron."""
from app import prune_old_records

if __name__ == "__main__":
    prune_old_records()
    print("Expired moderation and quota records purged")
