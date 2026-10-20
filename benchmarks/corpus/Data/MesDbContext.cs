using System.Data.Entity;
using Acme.Mes.Models;

namespace Acme.Mes.Data
{
    public class MesDbContext : DbContext
    {
        public DbSet<Lot> Lots { get; set; }
        public DbSet<ScrapRecord> ScrapRecords { get; set; }

        protected override void OnModelCreating(DbModelBuilder mb)
        {
            mb.Entity<Lot>().ToTable("MES_Lot").HasKey(l => l.LotNo);
            mb.Entity<ScrapRecord>().ToTable("MES_ScrapRecord").HasKey(s => s.Id);
        }
    }
}
