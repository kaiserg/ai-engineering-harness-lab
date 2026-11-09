"""Tráfico continuo y conexión TLS abierta durante renovación sin recreaciones."""
import http.client
import json
import socket
import threading
import time
import unittest

from cryptography import x509
from test_stack import docker
from test_rotation import maintenance, token, mcp_list
from harness.service_tls import trust_context


def certificate(port):
    with socket.create_connection(('127.0.0.1',port),timeout=3) as sock:
        with trust_context().wrap_socket(sock,server_hostname='127.0.0.1') as tls:
            return x509.load_der_x509_certificate(tls.getpeercert(binary_form=True)).serial_number


class LiveComposeTests(unittest.TestCase):
    def test_continuous_requests_and_open_tls_connection_survive_renewal(self):
        ids=docker('ps','-q','identity','jira-mcp','worker')
        before=certificate(8011)
        connection=http.client.HTTPSConnection('127.0.0.1',8011,context=trust_context(),timeout=3)
        self.addCleanup(connection.close)
        connection.request('GET','/jwks')
        response=connection.getresponse()
        self.assertEqual(response.status,200)
        response.read()
        original_socket=connection.sock
        samples=[]
        failures=[]
        stopping=threading.Event()
        ready=threading.Event()
        def traffic():
            while not stopping.is_set():
                try:
                    mcp_list(token())
                    samples.append(time.monotonic())
                    ready.set()
                except Exception as error:
                    failures.append(type(error).__name__)
                stopping.wait(.05)
        thread=threading.Thread(target=traffic,daemon=True)
        thread.start()
        try:
            self.assertTrue(ready.wait(5))
            changed=maintenance('renew-certificates')
            connection.request('GET','/jwks')
            response=connection.getresponse()
            self.assertEqual(response.status,200)
            response.read()
            self.assertIs(connection.sock,original_socket)
            self.assertEqual(certificate(8011),int(changed['certificates']['identity']['serial']))
            self.assertNotEqual(before,certificate(8011))
            time.sleep(.4)
        finally:
            stopping.set()
            thread.join(timeout=5)
        self.assertGreaterEqual(len(samples),3)
        self.assertEqual(failures,[])
        self.assertEqual(ids,docker('ps','-q','identity','jira-mcp','worker'))
