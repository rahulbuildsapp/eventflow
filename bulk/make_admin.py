"""
Create (or reset) the admin account for the bulk-data playground.

The dataset is 100% synthetic and contains no staff user, so /admin/ is
unusable until one exists. Run this after every `--truncate` reload.

    bulk/venv/bin/python bulk/make_admin.py
    bulk/venv/bin/python bulk/make_admin.py --email you@x.com

Note on USERNAME_FIELD
----------------------
rsvp/settings.py sets `USERNAME_FIELD = 'email'` at module level, which Django
ignores -- the setting only takes effect as an attribute on the *model*. Since
CustomUser subclasses AbstractUser without overriding it, the effective
USERNAME_FIELD stays 'username'. So the admin login is:

    username: admin
    password: admin123

...not the email address, despite what settings.py implies.
"""

import argparse
import getpass
import os
import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rsvp.settings_bulk")

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--username", default="admin")
    p.add_argument("--email", default="admin@example.com")
    p.add_argument("--password", default=None,
                   help="omit to be prompted interactively")
    args = p.parse_args()

    User = get_user_model()
    password = args.password or getpass.getpass("admin password: ")

    user, created = User.objects.get_or_create(
        username=args.username,
        defaults={"email": args.email},
    )
    user.email = args.email
    user.is_staff = True
    user.is_superuser = True
    user.is_active = True
    user.set_password(password)
    user.save()

    print(f"admin {'created' if created else 'updated'}: "
          f"username={user.username} email={user.email}")
    print(f"total users in dataset: {User.objects.count()}")
    print("\nlog in at http://127.0.0.1:8000/admin/login/ "
          f"with username={user.username!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())