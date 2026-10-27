from django.test import RequestFactory, SimpleTestCase, override_settings

from crudkit_sign.throttle import client_ip

factory = RequestFactory()


class ClientIpTests(SimpleTestCase):
    def test_forwarded_header_is_ignored_by_default(self):
        request = factory.get("/", HTTP_X_FORWARDED_FOR="6.6.6.6", REMOTE_ADDR="10.0.0.1")
        self.assertEqual(client_ip(request), "10.0.0.1")

    @override_settings(CRUDKIT_SIGN={"trusted_proxy_hops": 1})
    def test_trusted_hop_takes_the_value_the_proxy_appended(self):
        request = factory.get("/", HTTP_X_FORWARDED_FOR="6.6.6.6, 192.0.2.7", REMOTE_ADDR="10.0.0.1")
        self.assertEqual(client_ip(request), "192.0.2.7")

    @override_settings(CRUDKIT_SIGN={"trusted_proxy_hops": 2})
    def test_more_hops_than_entries_uses_the_first_entry(self):
        request = factory.get("/", HTTP_X_FORWARDED_FOR="192.0.2.7", REMOTE_ADDR="10.0.0.1")
        self.assertEqual(client_ip(request), "192.0.2.7")

    @override_settings(CRUDKIT_SIGN={"trusted_proxy_hops": 1})
    def test_no_header_falls_back_to_remote_addr(self):
        request = factory.get("/", REMOTE_ADDR="10.0.0.1")
        self.assertEqual(client_ip(request), "10.0.0.1")
