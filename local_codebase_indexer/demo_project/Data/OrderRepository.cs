using System.Data;
using Dapper;
using Shop.Models;

namespace Shop.Data
{
    public class OrderRepository
    {
        private readonly IDbConnection _db;

        public OrderRepository(IDbConnection db)
        {
            _db = db;
        }

        public Order Get(int id)
        {
            return _db.QueryFirstOrDefault<Order>(
                "SELECT Id, Status FROM dbo.Sales_OrderHeader WHERE Id = @Id", new { Id = id });
        }

        public void Approve(int id)
        {
            _db.Execute("dbo.usp_ApproveOrder", new { Id = id },
                commandType: CommandType.StoredProcedure);
        }
    }
}
