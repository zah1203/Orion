import shlex
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts.deploy_diagnostics import command, diagnose

INSTANCE='i-123abc'
ID='12345678-abcd-1234-abcd-123456789012'


class DiagnosticTests(unittest.TestCase):
    def test_identifiers_cannot_inject_shell_or_paths(self):
        for instance,identifier in ((INSTANCE,ID+'; false'),('../other',ID),(INSTANCE,'../../secret')):
            with self.assertRaises(ValueError): command(instance,identifier)
        words=shlex.split(command(INSTANCE,ID))
        self.assertEqual(words[:2],['/opt/orion/deploy-venv/bin/python','-c'])
        compile(words[2],'<diagnostic>','exec')
        self.assertIn('/document/orchestration/'+ID,words[2])

    def test_running_or_successful_command_is_not_retried_or_inspected(self):
        for state in ('InProgress','Success','Pending'):
            ssm=Mock(); ssm.get_command_invocation.return_value={'Status':state}
            with self.assertRaises(SystemExit): diagnose(ssm,INSTANCE,ID)
            ssm.send_command.assert_not_called()

    def test_failed_command_gets_only_one_readonly_diagnostic(self):
        ssm=Mock()
        ssm.get_command_invocation.side_effect=[{'Status':'Failed'},{'Status':'Success','StandardOutputContent':'safe output'}]
        ssm.send_command.return_value={'Command':{'CommandId':'probe'}}
        with patch('scripts.deploy_diagnostics.time.sleep'): diagnose(ssm,INSTANCE,ID)
        self.assertEqual(ssm.send_command.call_count,1)
        self.assertEqual(ssm.send_command.call_args.kwargs['Parameters']['commands'],[command(INSTANCE,ID)])
