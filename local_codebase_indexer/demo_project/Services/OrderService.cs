using Shop.Data;
using Shop.Models;

namespace Shop.Services
{
    public class OrderService : IOrderService
    {
        private readonly OrderRepository _repository;

        public OrderService(OrderRepository repository)
        {
            _repository = repository;
        }

        public Order Get(int id)
        {
            return _repository.Get(id);
        }

        public void Approve(int id)
        {
            _repository.Approve(id);
        }
    }
}
