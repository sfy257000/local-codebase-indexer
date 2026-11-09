using Shop.Models;
using Shop.Services;

namespace Shop.Controllers
{
    public class OrdersController
    {
        private readonly IOrderService _service;

        public OrdersController(IOrderService service)
        {
            _service = service;
        }

        public Order Details(int id)
        {
            return _service.Get(id);
        }

        public string Approve(int id)
        {
            _service.Approve(id);
            return "Approved";
        }
    }
}
