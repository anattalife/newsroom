"""Command line.

  python -m app serve            run the website (development server)
  python -m app worker           run the background worker
  python -m app reset-password EMAIL    set a new password for a user (if you're locked out)
  python -m app backup           make a backup zip now
"""
import getpass
import sys


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "serve":
        from . import create_app
        create_app().run(host="0.0.0.0", port=8000, debug=False)
    elif cmd == "worker":
        from .worker import run
        run()
    elif cmd == "backup":
        from . import db
        db.init()
        from .worker import make_backup
        print(make_backup("manual"))
    elif cmd == "reset-password" and len(sys.argv) > 2:
        from . import db as dbm
        from .security import hash_password, password_problem
        dbm.init()
        d = dbm.DB()
        u = d.one("SELECT * FROM users WHERE email=?", (sys.argv[2],))
        if not u:
            sys.exit("No user with that email.")
        pw = getpass.getpass("New password: ")
        if password_problem(pw):
            sys.exit(password_problem(pw))
        d.update("users", u["id"], password_hash=hash_password(pw), active=1)
        print("Password changed.")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
