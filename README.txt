This script is a service that maintains an updated 3LE satellite catalog,
periodically querying GP data from space-track.org

Usage:
    python satcat_mgr.py <catalog_file> <secrets_file>

Secrets file should be an INI format file containing a section like:

...
["space-track.org"]
username=ABC
password=XYZ
...

Also included here is an example systemd user service file.

