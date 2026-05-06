# demo.py

import pydantic_v1
import pydantic_v2

print("Versions:")
print("v1:", pydantic_v1.__version__)
print("v2:", pydantic_v2.__version__)

class UserV1(pydantic_v1.BaseModel):
    name: str

class UserV2(pydantic_v2.BaseModel):
    name: str

print(UserV1(name="Alice"))
print(UserV2(name="Bob"))
