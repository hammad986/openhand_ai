#!/usr/bin/env python3
"""
promote_admin.py — First-time Admin Setup Script
=================================================
Usage:
    python promote_admin.py <email_or_username> [role]

Role defaults to 'super_admin'. Valid roles: super_admin, admin, support
"""

import sys
import sqlite3

DB_PATH = "saas_platform.db"


def promote(identifier: str, role: str = "super_admin"):
    valid_roles = {"super_admin", "admin", "support", "user"}
    if role not in valid_roles:
        print(f"ERROR: Invalid role '{role}'. Must be one of: {', '.join(valid_roles)}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Try email first, then username
    c.execute("SELECT id, username, email FROM users WHERE email = ? OR username = ?", (identifier, identifier))
    row = c.fetchone()

    if not row:
        print(f"ERROR: No user found with email or username: '{identifier}'")
        print("\nExisting users:")
        c.execute("SELECT id, username, email, role FROM users ORDER BY id")
        for r in c.fetchall():
            print(f"  [{r[0]}] {r[1]} / {r[2]} — role: {r[3] or 'user'}")
        conn.close()
        sys.exit(1)

    uid, username, email = row
    c.execute("UPDATE users SET role = ? WHERE id = ?", (role, uid))
    conn.commit()
    conn.close()

    print(f"✅ User '{username}' (#{uid}, {email}) promoted to role: {role}")
    print(f"\nThey can now sign in at /admin with their existing credentials.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python promote_admin.py <email_or_username> [role]")
        print("       role defaults to super_admin")
        print("\nExample:")
        print("  python promote_admin.py admin@example.com super_admin")
        sys.exit(1)

    identifier = sys.argv[1]
    role = sys.argv[2] if len(sys.argv) > 2 else "super_admin"
    promote(identifier, role)
