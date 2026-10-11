import logging

from telegram import Update

from .bot import build_application
from .config import Settings


def main():
    settings = Settings.from_env()
    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                        level=settings.log_level)
    logging.getLogger('httpx').setLevel(logging.WARNING)

    app = build_application(settings)
    app.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])


if __name__ == '__main__':
    main()
