class IngestionError(ValueError):
    """Safe operator-facing error; never include parser/driver payloads."""
    def __init__(self,code,message):
        self.code=code;self.message=message;super().__init__(code)
