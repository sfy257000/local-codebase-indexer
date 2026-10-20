using System;
using System.Security.Cryptography;
using System.Text;

namespace Acme.Mes.Auth
{
    public class LoginService
    {
        public bool Authenticate(string userId, string password, string storedHash)
        {
            if (string.IsNullOrEmpty(userId) || string.IsNullOrEmpty(password))
                return false;
            return HashPassword(password) == storedHash;
        }

        private static string HashPassword(string password)
        {
            using (var sha = SHA256.Create())
            {
                var bytes = sha.ComputeHash(Encoding.UTF8.GetBytes(password));
                return Convert.ToBase64String(bytes);
            }
        }
    }
}
