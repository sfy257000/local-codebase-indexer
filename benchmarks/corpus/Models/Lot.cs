using System;

namespace Acme.Mes.Models
{
    public class Lot
    {
        public string LotNo { get; set; }
        public string PartNo { get; set; }
        public int Layers { get; set; }
        public string Status { get; set; }
        public DateTime CreatedAt { get; set; }
    }
}
