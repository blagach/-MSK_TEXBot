"""Persistent database location for provider self-service data.

The legacy bot keeps its existing leads database unchanged. Provider profiles
and equipment are stored on Render's persistent disk.
"""
DB_PATH = "/var/data/bot.db"
