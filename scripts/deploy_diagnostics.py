"""Read one failed deployment's retained SSM stderr and service state. No retry."""
import os
import re
import shlex
import time


def command(instance, command_id):
    if not re.fullmatch(r'i-[0-9a-f]+', instance) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', command_id):
        raise ValueError('Exact instance and SSM command IDs required')
    # Read only the named invocation's retained output; never dump environment,
    # portal configuration, credential files or application databases.
    source = '''from pathlib import Path
import subprocess
root = Path(PATH)
files = sorted(root.rglob('stderr')) if root.is_dir() else []
if len(files) > 5:
    raise SystemExit('Unexpected invocation log inventory')
for path in files:
    if path.is_symlink():
        raise SystemExit('Refusing symlink log')
    with path.open('rb') as stream:
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(max(0, size-10000))
        print(stream.read().decode('utf-8', errors='replace'))
if not files:
    print('Retained stderr not found')
print('Current release:', Path('/opt/orion/current').resolve())
for unit in ('orion-portal.service','orion-supervisor.service','orion-attention.service'):
    print(unit)
    subprocess.run(['systemctl','show',unit,'--property=MainPID,ActiveState,InvocationID,WorkingDirectory','--no-pager'],check=True)
'''.replace('PATH',repr('/var/lib/amazon/ssm/'+instance+'/document/orchestration/'+command_id))
    return '/opt/orion/deploy-venv/bin/python -c '+shlex.quote(source)


def diagnose(ssm, instance, command_id):
    payload=command(instance,command_id)
    original=ssm.get_command_invocation(CommandId=command_id,InstanceId=instance)
    if original['Status'] not in ('Failed','Cancelled','TimedOut'):
        raise SystemExit('Only a completed failed deployment can be inspected')
    print('Original deployment status:',original['Status'],flush=True)
    sent=ssm.send_command(InstanceIds=[instance],DocumentName='AWS-RunShellScript',
        Parameters={'commands':[payload],'executionTimeout':['60']},TimeoutSeconds=60)
    probe=sent['Command']['CommandId']
    print('Read-only diagnostic SSM command:',probe,flush=True)
    for _ in range(30):
        time.sleep(3)
        try:
            result=ssm.get_command_invocation(CommandId=probe,InstanceId=instance)
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if result['Status'] in ('Pending','InProgress','Delayed'): continue
        print(result.get('StandardOutputContent',''))
        print(result.get('StandardErrorContent',''))
        if result['Status']!='Success': raise SystemExit('Diagnostics failed; inspect invocation')
        return
    raise SystemExit('Diagnostics timed out; inspect invocation before retrying')
