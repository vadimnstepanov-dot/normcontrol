import os,sys
import importlib.util
from pathlib import Path
if importlib.util.find_spec('django') is None:sys.path.insert(0,str(Path(__file__).parent/'.deps'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE','portal.settings')
from django.core.management import execute_from_command_line
execute_from_command_line(sys.argv)
