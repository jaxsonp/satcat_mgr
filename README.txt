This script is a service that maintains an updated 3LE satellite catalog,
periodically querying GP data from space-track.org

Usage:
    python satcat_mgr.py <out_dir> <secrets_file>

This script will place a file 'full_catalog.3le' in the out_dir, as well as a
sub-directory called 'by

Secrets file should be an INI format file containing a section like:

...
["space-track.org"]
username=ABC
password=XYZ
...

Also included here is an example systemd user service file.


--- TODO ---

 * Don't query at peak times, as API docs suggest
