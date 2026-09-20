"""Chat front ends.

``common`` holds the command layer — parsing, API calls and message formatting —
so the Telegram and Slack bots expose exactly the same commands. Each bot module
only deals with its platform: authentication, receiving messages, and delivering
the outbound queue.
"""
