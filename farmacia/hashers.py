from django.contrib.auth.hashers import PBKDF2PasswordHasher


class FarmaciaPBKDF2PasswordHasher(PBKDF2PasswordHasher):
    iterations = 1_200_000
