/* Optional endpoint map census, taken after the final phase timestamp.
 * These are endpoint mappings, not a complete mapping lifetime trace. */
#ifdef __APPLE__
#include <mach-o/dyld.h>
#include <mach-o/loader.h>
struct fault_region { uint64_t start, end; int external; };
static struct fault_region fault_regions[65536];
static unsigned fault_region_count;
static int fault_region_error;
static void fault_regions_capture(void) {
    if (!getenv("LEAN_FAULT_REGIONS")) return;
    mach_vm_address_t a=0; mach_vm_size_t size; natural_t depth=0;
    for (;;) {
        vm_region_submap_info_data_64_t info;
        mach_msg_type_number_t count=VM_REGION_SUBMAP_INFO_COUNT_64;
        kern_return_t k=mach_vm_region_recurse(mach_task_self(),&a,&size,&depth,(vm_region_recurse_info_t)&info,&count);
        if (k==KERN_INVALID_ADDRESS) break;
        if (k!=KERN_SUCCESS) { fault_region_error=1; break; }
        if (info.is_submap) { depth++; continue; }
        if (fault_region_count==65536) { fault_region_error=1; break; }
        fault_regions[fault_region_count++]=(struct fault_region){a,a+size,info.external_pager};
        a+=size;
    }
}
static void fault_regions_report(FILE *f) {
    fprintf(f,"error %d\n",fault_region_error);
    for (unsigned i=0;i<fault_region_count;i++) {
        struct fault_region *r=&fault_regions[i];
        fprintf(f,"%llx %llx %s\n",(unsigned long long)r->start,(unsigned long long)r->end,r->external ? "other-file" : "anonymous");
    }
    for (uint32_t i=0;i<_dyld_image_count();i++) {
        const struct mach_header_64 *h=(const struct mach_header_64 *)_dyld_get_image_header(i);
        if (h->magic!=MH_MAGIC_64) continue;
        const struct load_command *lc=(const struct load_command *)(h+1);
        for (uint32_t j=0;j<h->ncmds;j++) {
            if (lc->cmd==LC_SEGMENT_64) {
                const struct segment_command_64 *s=(const struct segment_command_64 *)lc;
                if (s->vmsize && strcmp(s->segname,"__PAGEZERO")) {
                    uint64_t a=s->vmaddr+_dyld_get_image_vmaddr_slide(i);
                    fprintf(f,"%llx %llx native-image\n",(unsigned long long)a,(unsigned long long)(a+s->vmsize));
                }
            }
            lc=(const struct load_command *)((const char *)lc+lc->cmdsize);
        }
    }
}
#else
static char fault_proc_maps[16*1024*1024];
static size_t fault_proc_maps_size;
static int fault_region_error;
static void fault_regions_capture(void) {
    if (!getenv("LEAN_FAULT_REGIONS")) return;
    FILE *f=fopen("/proc/self/maps","r");
    if (!f) { fault_region_error=1; return; }
    fault_proc_maps_size=fread(fault_proc_maps,1,sizeof(fault_proc_maps),f);
    fault_region_error=ferror(f) || fault_proc_maps_size==sizeof(fault_proc_maps);
    fclose(f);
}
static void fault_regions_report(FILE *f) {
    fprintf(f,"error %d\n",fault_region_error);
    fwrite(fault_proc_maps,1,fault_proc_maps_size,f);
}
#endif
