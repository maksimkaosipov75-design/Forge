import unittest

from core.event_protocol import decode_forge_event, encode_forge_event, extract_forge_event


class EventProtocolTests(unittest.TestCase):
    def test_encode_and_decode_roundtrip(self):
        line = encode_forge_event(
            "question",
            text="Need API key?",
            title="Authorization",
            options=[{"id": "yes", "label": "API key"}],
        )

        payload = decode_forge_event(line)

        self.assertIsNotNone(payload)
        self.assertEqual(payload["type"], "question")
        self.assertEqual(payload["title"], "Authorization")
        self.assertEqual(payload["text"], "Need API key?")

    def test_extract_forge_event_supports_embedded_payload(self):
        payload = {"type": "assistant", "forge_event": {"type": "approval", "text": "Allow shell?"}}

        event = extract_forge_event(payload)

        self.assertEqual(event, {"type": "approval", "text": "Allow shell?"})


if __name__ == "__main__":
    unittest.main()
