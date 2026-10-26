#include "common.h"

extern s16 D_80800030_seqdat[];

s32 seqdat_entrypoint_0(s32 arg0, s32 arg1) {
    if (arg1 == 0) {
        return D_80800030_seqdat[arg0];
    }
    return 0;
}
