namespace Acme.Mes.Models
{
    public class ScrapRecord
    {
        public int Id { get; set; }
        public string LotNo { get; set; }
        public string ReasonCode { get; set; }
        public int Qty { get; set; }
        public string OperatorId { get; set; }
    }
}
