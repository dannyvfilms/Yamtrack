# Running python commands

To run python commands to test things, you can do like this:

Linux:

```bash
. venv/bin/activate && cd src && python manage.py shell -c "
print('Test')
"
```