import tgcrypto


def ctr256_encrypt(data, key, iv, state=None):
    return tgcrypto.ctr256_encrypt(data, key, iv, state or bytearray(1))


def ctr256_decrypt(data, key, iv, state=None):
    return tgcrypto.ctr256_decrypt(data, key, iv, state or bytearray(1))


def ige256_encrypt(data, key, iv):
    return tgcrypto.ige256_encrypt(data, key, iv)


def ige256_decrypt(data, key, iv):
    return tgcrypto.ige256_decrypt(data, key, iv)
