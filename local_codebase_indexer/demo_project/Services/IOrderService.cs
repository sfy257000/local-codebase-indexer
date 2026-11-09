using Shop.Models;

namespace Shop.Services
{
    public interface IOrderService
    {
        Order Get(int id);
        void Approve(int id);
    }
}
