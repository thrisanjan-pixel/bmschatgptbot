"""Single-line structured stdout logs preserve severity in Railway."""
import json
import logging
import sys


class JsonFormatter(logging.Formatter):
    def format(self,record):
        item={'level':'warn' if record.levelno==logging.WARNING else record.levelname.lower(),
              'message':record.getMessage(),'logger':record.name,'at':record.created}
        if record.exc_info:item['traceback']=self.formatException(record.exc_info)
        return json.dumps(item,ensure_ascii=True,separators=(',',':'))


def configure():
    handler=logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(lambda record:record.levelno<logging.ERROR)
    errors=logging.StreamHandler(sys.stderr)
    errors.setLevel(logging.ERROR)
    errors.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO,handlers=[handler,errors])
