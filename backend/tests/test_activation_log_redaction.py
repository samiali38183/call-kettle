"""Invitation bearer tokens must never appear in access/application logs."""
import logging
from app import ops


def test_activation_tokens_are_redacted_in_message_and_access_log_args():
    bearer = 'fictional-test-invitation-bearer'
    path = f'/portal/activate?token={bearer}&x=1'
    record = logging.LogRecord('uvicorn.access', logging.INFO, '', 0, '%s', (path,), None)
    ops.RedactKeys().filter(record)
    assert bearer not in record.getMessage()
    assert 'token=REDACTED&x=1' in record.getMessage()
    direct = logging.LogRecord('callkettle.main', logging.INFO, '', 0, path, (), None)
    ops.RedactKeys().filter(direct)
    assert bearer not in direct.getMessage()
